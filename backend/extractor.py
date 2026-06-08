import re
import os
import zipfile
import logging
import math
from typing import Dict, List, Tuple, Optional, Any
import fitz  # PyMuPDF
from collections import Counter

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AnnualReportExtractor")

def normalize_text(text: str) -> str:
    """Normalise text for case, whitespace, and common OCR/extraction errors."""
    if not text:
        return ""
    # Lowercase & standardise whitespace
    normalized = " ".join(text.lower().split())
    
    # Common OCR error replacements
    replacements = {
        "prcrfit": "profit",
        "pr0fit": "profit",
        "prcfit": "profit",
        "st&ndalone": "standalone",
        "c0nsolidated": "consolidated",
        "c0ns0lidated": "consolidated",
        "b&lance": "balance",
        "sh33t": "sheet",
        "c&sh": "cash",
        "fl0w": "flow",
        "loss a/c": "loss account",
        "p&l": "profit and loss",
        "p & l": "profit and loss",
        "&": "and",
    }
    for old, new in replacements.items():
        if old.isalnum() or old.replace('_', '').isalnum():
            normalized = re.sub(r'\b' + re.escape(old) + r'\b', new, normalized)
        else:
            normalized = normalized.replace(old, new)
        
    return normalized.strip()

class AnnualReportExtractor:
    def __init__(self, pdf_path: str, progress_callback=None):
        self.pdf_path = pdf_path
        self.progress_callback = progress_callback
        
        # Verify the file is readable with PyMuPDF
        self.doc = fitz.open(pdf_path)
        self.total_pages = len(self.doc)
        self.toc_data: List[Dict[str, Any]] = []
        
        # Auto-detect report type (single_page or double_page)
        self.report_type = self.auto_detect_report_type()
        self.scale = 1.0 if self.report_type == "single_page" else 0.5
        self.offset = 0
        
    def auto_detect_report_type(self) -> str:
        """
        Auto-recognise if the PDF is a single-page report or a 2-page (double-page spread) report.
        Returns "single_page" or "double_page".
        """
        try:
            if self.total_pages > 0:
                # Check first 5 pages for aspect ratio
                landscape_count = 0
                check_pages = min(5, self.total_pages)
                for p in range(check_pages):
                    rect = self.doc[p].rect
                    # If width is significantly larger than height (e.g. landscape)
                    if rect.width > 1.2 * rect.height:
                        landscape_count += 1
                if landscape_count >= check_pages / 2.0:
                    logger.info("Auto-recognised report type: double_page (landscape/double spread)")
                    return "double_page"
        except Exception as e:
            logger.warning(f"Error auto-detecting report type: {e}")
        logger.info("Auto-recognised report type: single_page (portrait)")
        return "single_page"

    def update_progress(self, percent: int, step: str):
        if self.progress_callback:
            self.progress_callback(percent, step)
        logger.info(f"Progress: {percent}% - {step}")

    def scan_toc(self) -> List[Dict[str, Any]]:
        """
        Scan pages 1-40 for Table of Contents.
        Try in order:
        1. Text-based detection on pages 1-40.
        2. Layout-based detection using pdfplumber word coordinates.
        3. OCR fallback (pytesseract + pdf2image) if no text layer is present.
        """
        limit = min(40, self.total_pages)
        self.update_progress(5, "Scanning for Table of Contents pages...")
        
        # Step 0: Check for native PDF bookmarks first
        try:
            native_toc = self.doc.get_toc()
            if native_toc and len(native_toc) > 1:
                logger.info(f"Found native PDF outline (bookmarks) with {len(native_toc)} entries.")
                entries = []
                active_path = {}
                for level, title, page in native_toc:
                    title_clean = title.strip()
                    if not title_clean or page <= 0 or page > self.total_pages:
                        continue
                    
                    # Track hierarchy
                    active_path[level] = title_clean
                    # Clean up keys for deeper levels if we popped back up
                    for lvl in list(active_path.keys()):
                        if lvl > level:
                            active_path.pop(lvl, None)
                            
                    # Construct full hierarchical title
                    path_parts = [active_path[lvl] for lvl in sorted(active_path.keys())]
                    full_title = " - ".join(path_parts)
                    
                    entries.append({
                        "section_name": full_title,
                        "page_number": page,
                        "source_page": page
                    })
                if len(entries) > 1:
                    self.toc_data = entries
                    self.update_progress(50, f"Native PDF TOC parsed. Found {len(entries)} entries.")
                    self.scale = 1.0
                    self.offset = 0
                    logger.info("Using native PDF bookmarks. Document geometry set to scale=1.0, offset=0.")
                    return self.toc_data
        except Exception as e:
            logger.warning(f"Failed to extract native PDF outline: {e}")
            
        # Step 1: Detect which pages are likely TOC pages
        toc_pages = []
        toc_keywords = ["table of contents", "contents", "index", "annual report index", "sr. no.", "particulars", "page no"]
        
        for page_idx in range(limit):
            page = self.doc[page_idx]
            text = page.get_text()
            if not text:
                continue
            
            text_lower = text.lower()
            # If page contains typical TOC keywords
            matches_keyword = any(kw in text_lower for kw in toc_keywords)
            # TOCs typically have dot leaders or multiple numbers
            dots_count = text.count("...") + text.count(".. ") + text.count(". .")
            lines_with_numbers = len(re.findall(r'\b\d+\b\s*$', text, re.MULTILINE))
            
            if matches_keyword or (dots_count > 5 and lines_with_numbers > 3):
                toc_pages.append(page_idx)
                
        logger.info(f"Detected potential TOC pages: {[p+1 for p in toc_pages]}")
        
        # If no text pages could be identified, check if it's a scanned PDF
        is_scanned = len(toc_pages) == 0 and all(not self.doc[p].get_text().strip() for p in range(limit))
        
        if is_scanned:
            self.update_progress(10, "Scanned PDF detected. OCR is disabled.")
            return []
            
        if not toc_pages:
            # If no specific TOC pages detected but there is text, scan all first 40 pages
            toc_pages = list(range(limit))

        # Try Layout-based parsing with pdfplumber on detected TOC pages
        self.update_progress(20, "Extracting layout-based TOC structure...")
        parsed_entries = self.parse_toc_layout(toc_pages)
        
        # If layout parsing yielded very few results, fallback to simple Text-based regex parsing
        if len(parsed_entries) < 5:
            self.update_progress(35, "Layout-based parsing returned few results. Trying text-based regex...")
            parsed_entries = self.parse_toc_text_regex(toc_pages)
            
        self.toc_data = parsed_entries
        self.update_progress(50, f"TOC parsed. Found {len(parsed_entries)} entries.")
        
        # Attempt to auto-detect page geometry (scale and offset)
        self.detect_scale_and_offset()
        
        return self.toc_data

    def parse_toc_layout(self, toc_pages: List[int]) -> List[Dict[str, Any]]:
        """
        Identify tabular structures with left-aligned labels and right-aligned page numbers
        using pdfplumber's word extraction.
        """
        entries = []
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                for page_idx in toc_pages:
                    if page_idx >= len(pdf.pages):
                        continue
                    plumb_page = pdf.pages[page_idx]
                    words = plumb_page.extract_words()
                    if not words:
                        continue
                    
                    # Group words into lines based on vertical tolerance (y0/y1)
                    # Rounding y0 to nearest integer (or grouping within 3pt)
                    lines_dict = {}
                    for w in words:
                        y_center = (w["top"] + w["bottom"]) / 2.0
                        # Find an existing line within 3.5 points vertical distance
                        found_line = None
                        for y_key in lines_dict.keys():
                            if abs(y_center - y_key) < 3.5:
                                found_line = y_key
                                break
                        if found_line is not None:
                            lines_dict[found_line].append(w)
                        else:
                            lines_dict[y_center] = [w]
                            
                    # Sort words in each line horizontally (by x0)
                    for y_key in sorted(lines_dict.keys()):
                        line_words = sorted(lines_dict[y_key], key=lambda x: x["x0"])
                        
                        # Process line words to extract labels and page numbers
                        # Also handle multi-column layouts!
                        line_entries = self.parse_words_line(line_words, page_idx + 1)
                        entries.extend(line_entries)
        except Exception as e:
            logger.error(f"Error in layout-based parsing: {e}", exc_info=True)
            
        return entries

    def parse_words_line(self, words: List[Dict[str, Any]], physical_page: int) -> List[Dict[str, Any]]:
        """
        Parses a list of words on a single horizontal line into one or more TOC entries.
        Supports multi-column layouts by segmenting the line based on spacing and numbers.
        """
        if not words:
            return []
            
        # Reconstruct full text to check for dot leaders or separators
        line_text = " ".join(w["text"] for w in words)
        
        # Check if line contains a number at all
        has_number = any(w["text"].isdigit() for w in words)
        if not has_number:
            return []
            
        # Segment words into columns/blocks
        # A new block starts if there is a large horizontal gap (e.g. > 30 points)
        # OR if we see a number followed by text (which indicates transition to next column)
        blocks = []
        gaps = []
        for i in range(1, len(words)):
            gaps.append(words[i]["x0"] - words[i-1]["x1"])
            
        # Determine dynamic gap threshold
        if gaps:
            avg_gap = sum(gaps) / len(gaps)
            max_gap = max(gaps)
            threshold = max(15, avg_gap * 3)
            if max_gap > 35:
                threshold = max_gap * 0.5
        else:
            threshold = 35

        current_block = [words[0]]
        
        for i in range(1, len(words)):
            w_prev = words[i-1]
            w_curr = words[i]
            gap = w_curr["x0"] - w_prev["x1"]
            
            # If there's a huge gap or a number followed by a non-number, we split
            if gap > threshold or (w_prev["text"].isdigit() and not w_curr["text"].isdigit() and len(w_curr["text"]) > 1):
                blocks.append(current_block)
                current_block = [w_curr]
            else:
                current_block.append(w_curr)
        blocks.append(current_block)
        
        line_entries = []
        for block in blocks:
            if not block:
                continue
            
            # In a block, find the last number (or the only number)
            # Typically a TOC block is: Title .... PageNumber
            # Check from right to left in the block
            num_idx = -1
            for idx in range(len(block) - 1, -1, -1):
                if block[idx]["text"].isdigit():
                    num_idx = idx
                    break
                    
            if num_idx == -1:
                continue
                
            page_num_str = block[num_idx]["text"]
            page_number = int(page_num_str)
            
            # The title words are everything to the left of the number
            title_words = block[:num_idx]
            # Strip trailing dots or dashes from title words
            title_text = " ".join(w["text"] for w in title_words)
            title_clean = re.sub(r'[\.\-\s_#\s]+$', '', title_text).strip()
            
            # Avoid single character titles or section numbers only
            if len(title_clean) > 3 and not title_clean.isdigit():
                line_entries.append({
                    "section_name": title_clean,
                    "page_number": page_number,
                    "source_page": physical_page
                })
                
        return line_entries

    def parse_toc_text_regex(self, toc_pages: List[int]) -> List[Dict[str, Any]]:
        """Fallback text-based parser using regex matching on page text lines."""
        entries = []
        for page_idx in toc_pages:
            page = self.doc[page_idx]
            text = page.get_text()
            if not text:
                continue
                
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # Regex to find: Title [dots/dashes/spaces] PageNumber
                # e.g., "Standalone Balance Sheet .............. 154"
                match_end = re.search(r'^(.+?)(?:\.{2,}|[\s\.\-]+)(\d+)\s*$', line)
                # Or sometimes PageNumber [dots/dashes/spaces] Title
                match_start = re.search(r'^\s*(\d+)(?:\.{2,}|[\s\.\-]+)(.+?)$', line)
                
                title = ""
                page_num = -1
                
                if match_end:
                    title = match_end.group(1).strip()
                    page_num = int(match_end.group(2))
                elif match_start:
                    title = match_start.group(2).strip()
                    page_num = int(match_start.group(1))
                    
                # Clean up dots, lines, numbers
                if title and page_num > 0:
                    title_clean = re.sub(r'[\.\-\s_#\s]+$', '', title).strip()
                    title_clean = re.sub(r'^\s*[\.\-\s_#\s]+', '', title_clean).strip()
                    if len(title_clean) > 3 and not title_clean.isdigit():
                        entries.append({
                            "section_name": title_clean,
                            "page_number": page_num,
                            "source_page": page_idx + 1
                        })
        return entries

    def detect_scale_and_offset(self):
        """
        Auto-detect the page scale and offset between printed pages and PDF physical pages.
        For normal PDFs: scale = 1.0, physical = printed + offset - 1.
        For double-page spreads (e.g. side-by-side landscape pages): scale = 0.5, physical = int(printed * 0.5) + offset - 1.
        """
        if not self.toc_data:
            self.scale = 1.0 if self.report_type == "single_page" else 0.5
            self.offset = 0
            return
            
        # Candidate scales to check - restricted by report_type bifurcation
        candidate_scales = [1.0] if self.report_type == "single_page" else [0.5]
        scale_scores = {s: [] for s in candidate_scales}
        
        sample_entries = [e for e in self.toc_data if 5 < e["page_number"] < self.total_pages * 2 - 10]
        sample_entries = sample_entries[:12] # Limit to keep it fast
        
        # Check aspect ratio of first few pages to see if it is landscape
        is_landscape = self.report_type == "double_page"

        for s in candidate_scales:
            offsets = []
            for entry in sample_entries:
                printed_p = entry["page_number"]
                title_query = normalize_text(entry["section_name"])
                
                # Under scale s, the expected physical index (0-indexed) is:
                expected_phys_idx = int(printed_p * s)
                
                # Search a local window around expected index
                search_start = max(0, expected_phys_idx - 8)
                search_end = min(self.total_pages, expected_phys_idx + 18)
                
                for phys_idx in range(search_start, search_end):
                    try:
                        page = self.doc[phys_idx]
                        page_text = page.get_text()
                        if not page_text:
                            continue
                        
                        page_text_norm = normalize_text(page_text)
                        
                        # Title is found on page
                        if title_query in page_text_norm:
                            # Offset = physical page (1-indexed) - int(printed * s)
                            offset_candidate = (phys_idx + 1) - int(printed_p * s)
                            offsets.append(offset_candidate)
                            break
                    except Exception:
                        pass
            scale_scores[s] = offsets
            
        # Select best scale based on number of matches found
        best_scale = 1.0 if self.report_type == "single_page" else 0.5
        best_offset = 0
        max_matches = -1
        
        for s in candidate_scales:
            matches_count = len(scale_scores[s])
            logger.info(f"Scale hypothesis {s}: found {matches_count} mapping matches.")
            if matches_count > max_matches:
                max_matches = matches_count
                best_scale = s
                if scale_scores[s]:
                    counter = Counter(scale_scores[s])
                    best_offset = counter.most_common(1)[0][0]
                else:
                    best_offset = 0
                    
        # If we got no matches at all, fallback to default
        if max_matches == 0:
            best_scale = 1.0 if self.report_type == "single_page" else 0.5
            best_offset = 0
            logger.info(f"No text matches found for offset. Defaulting scale to {best_scale}.")
        self.scale = best_scale
        self.offset = best_offset
        logger.info(f"Resolved document geometry: scale={self.scale}, offset={self.offset}")

    def check_notes_start(self, page_text: str) -> bool:
        """Check if page text indicates the start of the Notes to Accounts/Significant Accounting Policies."""
        if not page_text:
            return False
        text_norm = normalize_text(page_text)
        indicators = [
            "notes to the financial statements",
            "notes to financial statements",
            "notes forming part of the financial statements",
            "notes to the standalone financial statements",
            "notes to the consolidated financial statements",
            "significant accounting policies",
            "notes annexed to and forming part",
            "notes forming part of the standalone",
            "notes forming part of the consolidated",
            "notes to consolidated financial statements",
            "notes to standalone financial statements",
        ]
        if any(ind in text_norm for ind in indicators):
            return True
            
        # Also check if it starts with "Note 1" or "1. significant accounting"
        lines = page_text.split('\n')
        for line in lines[:10]: # Check first 10 lines of the page
            line_norm = normalize_text(line)
            if re.search(r'^\s*note\s+1\b', line_norm) or re.search(r'^\s*1\s+significant\s+accounting', line_norm):
                return True
        return False

    def check_mda_end(self, page_text: str) -> bool:
        """Check if page text indicates the start of a major section that typically follows MD&A."""
        if not page_text:
            return False
        indicators = [
            "directors' report",
            "director's report",
            "board's report",
            "board report",
            "report of directors",
            "corporate governance report",
            "independent auditor's report",
            "independent auditors' report",
            "balance sheet",
        ]
        lines = page_text.split('\n')
        for line in lines[:8]:
            line_norm = normalize_text(line)
            if any(ind == line_norm or line_norm.startswith(ind) for ind in indicators):
                return True
        return False

    def map_statements(self, toc_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Maps TOC entries to the six target financial statements.
        Returns a dict mapping statement types to resolved { start_page: int, end_page: int, name: str }.
        """
        # Exact keyword lists as defined in the requirements
        statement_keywords = {
            "consolidated_balance_sheet": ["Consolidated Balance Sheet", "Consolidated Statement of Assets and Liabilities"],
            "standalone_balance_sheet": ["Standalone Balance Sheet", "Balance Sheet"],
            "consolidated_pl": ["Consolidated Statement of Profit and Loss", "Consolidated P&L"],
            "standalone_pl": ["Statement of Profit and Loss", "Profit and Loss Account"],
            "consolidated_cash_flow": ["Consolidated Cash Flow Statement", "Consolidated Statement of Cash Flows"],
            "standalone_cash_flow": ["Cash Flow Statement", "Statement of Cash Flows"],
            "mda": ["Management Discussion and Analysis", "Management's Discussion and Analysis", "Management Discussion & Analysis", "MD&A"],
            "notes_to_accounts": ["Notes to the Financial Statements", "Notes to Consolidated Financial Statements", "Notes to Standalone Financial Statements", "Significant Accounting Policies", "Notes forming part of"],
            "directors_report": ["Board's Report", "Directors' Report", "Letter to Shareholders", "Message from the CEO", "Managing Director's Message", "Director's Report"],
            "auditors_report": ["Independent Auditor's Report", "Auditor's Report"]
        }
        
        # Prepare keyword match mappings (normalised versions)
        keyword_map = {}
        for key, kw_list in statement_keywords.items():
            keyword_map[key] = [normalize_text(kw) for kw in kw_list]
            
        mappings = {}
        
        # 1. Match TOC entries using normalised text
        matched_entries = {}
        for entry in toc_list:
            section_norm = normalize_text(entry["section_name"])
            
            for key, kw_list in keyword_map.items():
                # Check for direct match
                # Also handle minor errors in text or exact matches
                if key != "mda":
                    if "consolidated" in key and "consolidated" not in section_norm:
                        continue
                    if "standalone" in key and "consolidated" in section_norm:
                        continue
                        
                # Match if the section title is very similar to the keyword
                for kw in kw_list:
                    if kw in section_norm or section_norm in kw:
                        # If multiple TOC entries match, pick the one that is closer in length
                        # or has higher text similarity
                        if key not in matched_entries:
                            matched_entries[key] = entry
                        else:
                            # Prefer exact matching over partial
                            prev_entry = matched_entries[key]
                            prev_norm = normalize_text(prev_entry["section_name"])
                            if abs(len(section_norm) - len(kw)) < abs(len(prev_norm) - len(kw)):
                                matched_entries[key] = entry
                                
        # 2. If TOC match fails for any statement, attempt cross-validation from mirrored sections
        missing_keys = [k for k in statement_keywords.keys() if k not in matched_entries]
        
        pairs = [
            ("standalone_balance_sheet", "consolidated_balance_sheet"),
            ("standalone_pl", "consolidated_pl"),
            ("standalone_cash_flow", "consolidated_cash_flow")
        ]
        
        for st_k, con_k in pairs:
            if st_k in missing_keys and con_k in matched_entries:
                for other_st_k, other_con_k in pairs:
                    if other_st_k != st_k and other_st_k in matched_entries and other_con_k in matched_entries:
                        gap = matched_entries[other_con_k]["page_number"] - matched_entries[other_st_k]["page_number"]
                        inferred_page = matched_entries[con_k]["page_number"] - gap
                        if inferred_page > 0:
                            matched_entries[st_k] = {
                                "section_name": statement_keywords[st_k][0],
                                "page_number": inferred_page,
                                "source_page": matched_entries[con_k].get("source_page", 1),
                                "is_cross_validated": True
                            }
                            missing_keys.remove(st_k)
                            logger.info(f"Cross-validated {st_k} based on {con_k} and {other_st_k}/{other_con_k} offset {gap}")
                            break
            elif con_k in missing_keys and st_k in matched_entries:
                for other_st_k, other_con_k in pairs:
                    if other_st_k != st_k and other_st_k in matched_entries and other_con_k in matched_entries:
                        gap = matched_entries[other_con_k]["page_number"] - matched_entries[other_st_k]["page_number"]
                        inferred_page = matched_entries[st_k]["page_number"] + gap
                        if inferred_page > 0:
                            matched_entries[con_k] = {
                                "section_name": statement_keywords[con_k][0],
                                "page_number": inferred_page,
                                "source_page": matched_entries[st_k].get("source_page", 1),
                                "is_cross_validated": True
                            }
                            missing_keys.remove(con_k)
                            logger.info(f"Cross-validated {con_k} based on {st_k} and {other_st_k}/{other_con_k} offset {gap}")
                            break

        # 3. If still missing, fall back to a full-document keyword scan
        if missing_keys:
            logger.info(f"TOC match failed for: {missing_keys}. Falling back to full-document scan...")
            full_scan_results = self.fallback_full_scan(keyword_map, missing_keys)
            for key, phys_page in full_scan_results.items():
                # We need to map physical page back to printed page using our offset and scale
                printed_page = int((phys_page + 1 - self.offset) / self.scale)
                matched_entries[key] = {
                    "section_name": statement_keywords[key][0],
                    "page_number": printed_page,
                    "source_page": phys_page + 1,
                    "is_fallback": True
                }
                
        # 4. Calculate page ranges for each matched statement
        # Group target statements: Standalone together, Consolidated together, and MD&A separate.
        groups = {
            "standalone": ["standalone_balance_sheet", "standalone_pl", "standalone_cash_flow"],
            "consolidated": ["consolidated_balance_sheet", "consolidated_pl", "consolidated_cash_flow"],
            "notes": ["notes_to_accounts"],
            "mda": ["mda"],
            "directors_report": ["directors_report"],
            "auditors_report": ["auditors_report"]
        }
        
        # Resolve physical page indices (0-indexed)
        resolved_matches = {}
        for key, entry in matched_entries.items():
            printed_page = entry["page_number"]
            # Convert printed page to physical page (0-indexed)
            phys_page = int(printed_page * self.scale) + self.offset - 1
            phys_page = max(0, min(phys_page, self.total_pages - 1))
            resolved_matches[key] = {
                "name": entry["section_name"],
                "printed_page": printed_page,
                "physical_start": phys_page
            }
            
        # Define major section keywords to end MD&A
        major_section_keywords = [
            "board", "director", "governance", "responsibility", "brsr", "esg",
            "financial statement", "financial section", "independent auditor", "auditor's report", "auditors' report",
            "balance sheet", "profit", "loss", "p&l", "p & l", "cash flow", "notes", "accounting", "notice",
            "shareholder", "investor", "corporate overview", "company overview", "statutory", "corporate social responsibility"
        ]

        for group_name, keys in groups.items():
            # Get matches in this group
            group_matches = []
            for key in keys:
                if key in resolved_matches:
                    group_matches.append({
                        "key": key,
                        **resolved_matches[key]
                    })
                    
            # Sort group matches by physical start
            group_matches = sorted(group_matches, key=lambda x: x["physical_start"])
            
            for idx, match in enumerate(group_matches):
                key = match["key"]
                start_phys = match["physical_start"]
                printed_page = match["printed_page"]
                
                # A. Find the next matched statement in the *same* group (since they are strictly contiguous)
                next_group_match_phys = None
                if idx < len(group_matches) - 1:
                    next_group_match_phys = group_matches[idx+1]["physical_start"]
                
                # Determine end page candidate
                if key != "mda" and next_group_match_phys is not None:
                    # If this is not MDA and there is a next statement in the same contiguous group,
                    # the current statement ends EXACTLY where the next statement starts!
                    end_phys = next_group_match_phys
                else:
                    # If this is the last statement in a contiguous group (e.g. Cash Flow), or if it is MDA,
                    # we must look at the next TOC entry to determine where it ends.
                    next_toc_entry = None
                    sorted_all_toc = sorted(toc_list, key=lambda x: x["page_number"])
                    for entry in sorted_all_toc:
                        if entry["page_number"] > printed_page:
                            # For MD&A, we must skip subsections of MD&A
                            if key == "mda":
                                entry_norm = normalize_text(entry["section_name"])
                                # Skip if it is a subsection of MD&A itself
                                if any(kw in entry_norm for kw in ["management discussion", "management's discussion", "mdanda", "mda"]):
                                    continue
                                # Skip if it is within 15 pages of MD&A and is not a major section
                                dist_pages = entry["page_number"] - printed_page
                                if dist_pages <= 15:
                                    if not any(kw in entry_norm for kw in major_section_keywords):
                                        continue
                                        
                            next_toc_entry = entry
                            break
                    
                    if next_toc_entry is not None:
                        # Use relative page calculation based on printed page difference (invariant length)
                        printed_length = next_toc_entry["page_number"] - printed_page
                        if self.scale == 0.5:
                            # 2-page landscape report: length in physical pages
                            length_phys = math.ceil(next_toc_entry["page_number"] * 0.5) - int(printed_page * 0.5)
                        else:
                            # 1-page portrait report
                            length_phys = printed_length
                            
                        end_phys = start_phys + length_phys
                        logger.info(f"Resolved relative end page for {key}: printed length {printed_length} pages, physical length {length_phys} pages. end_phys = {end_phys}")
                    else:
                        # Fallback buffer if no subsequent TOC entries
                        buffer_size = 20 if key == "mda" else 3
                        if self.scale == 0.5:
                            buffer_phys = math.ceil(buffer_size * 0.5)
                        else:
                            buffer_phys = buffer_size
                        end_phys = min(self.total_pages, start_phys + buffer_phys)
                
                # B. Page-level scanning safeguards to truncate precisely before notes or next major sections
                if key in ["standalone_cash_flow", "consolidated_cash_flow"]:
                    # Scan subsequent pages for Notes-to-Accounts start to truncate cash flow statements precisely
                    scan_limit = min(self.total_pages, start_phys + 6) # check up to 5 pages after start_phys
                    for p in range(start_phys + 1, scan_limit):
                        try:
                            p_text = self.doc[p].get_text()
                            if self.check_notes_start(p_text):
                                logger.info(f"Notes-to-Accounts detected on page {p+1} via scanner. Truncating {key} at page {p}.")
                                if p < end_phys:
                                    end_phys = p
                                break
                        except Exception as e:
                            logger.error(f"Error scanning page {p} for Notes indicators: {e}")
                            
                elif key == "mda":
                    # Scan subsequent pages for a major section start (e.g. Board's Report) to truncate MD&A precisely
                    scan_limit = min(self.total_pages, start_phys + 26) # check up to 25 pages after start_phys
                    for p in range(start_phys + 1, scan_limit):
                        try:
                            p_text = self.doc[p].get_text()
                            if self.check_mda_end(p_text):
                                logger.info(f"Major section detected on page {p+1} via scanner. Truncating {key} at page {p}.")
                                if p < end_phys:
                                    end_phys = p
                                break
                        except Exception as e:
                            logger.error(f"Error scanning page {p} for MD&A end indicators: {e}")

                # C. Apply group-specific safeguard caps
                if key == "mda":
                    # Cap MD&A at 25 pages max to avoid eating subsequent chapters
                    if end_phys > start_phys + 25:
                        end_phys = start_phys + 25
                else:
                    # Cap Balance Sheets, P&L, and Cash Flow statements at 5 pages max to avoid notes
                    if end_phys > start_phys + 5:
                        end_phys = start_phys + 5
                        
                if end_phys <= start_phys:
                    end_phys = start_phys + 1
                    
                # Store ranges (1-indexed for the frontend)
                mappings[key] = {
                    "name": match["name"],
                    "start_page": start_phys + 1,  # 1-indexed
                    "end_page": end_phys,          # 1-indexed, inclusive is end_phys
                    "physical_range": (start_phys, end_phys)
                }
            
        return mappings

    def fallback_full_scan(self, keyword_map: Dict[str, List[str]], missing_keys: List[str]) -> Dict[str, int]:
        """Scan the entire document for missing statements headers using scoring heuristics."""
        results = {}
        
        # Scoring configuration matching our successful test
        rules = {
            "standalone_balance_sheet": {
                "title_kws": ["balance sheet", "statement of assets and liabilities"],
                "body_kws": ["non-current assets", "current assets", "share capital", "equity and liabilities", "as at 31st march", "as at march"],
                "not_kws": ["consolidated", "group", "notes to the", "notes forming part", "significant accounting policies", "cash flow from", "operating activities", "for the year ended", "earnings per share", "diluted eps"]
            },
            "consolidated_balance_sheet": {
                "title_kws": ["balance sheet", "statement of assets and liabilities"],
                "body_kws": ["non-current assets", "current assets", "share capital", "equity and liabilities", "consolidated", "as at 31st march", "as at march"],
                "not_kws": ["standalone", "notes to the", "notes forming part", "significant accounting policies", "cash flow from", "operating activities", "for the year ended", "earnings per share", "diluted eps"]
            },
            "standalone_pl": {
                "title_kws": ["profit and loss", "profit & loss", "statement of profit", "income statement"],
                "body_kws": ["revenue from operations", "diluted eps", "earnings per share", "finance costs", "for the year ended", "profit for the year"],
                "not_kws": ["consolidated", "group", "notes to the", "notes forming part", "significant accounting policies", "cash flow from", "operating activities", "investing activities", "financing activities", "as at 31st march", "as at march"]
            },
            "consolidated_pl": {
                "title_kws": ["profit and loss", "profit & loss", "statement of profit", "income statement"],
                "body_kws": ["revenue from operations", "diluted eps", "earnings per share", "finance costs", "consolidated", "for the year ended", "profit for the year"],
                "not_kws": ["standalone", "notes to the", "notes forming part", "significant accounting policies", "cash flow from", "operating activities", "investing activities", "financing activities", "as at 31st march", "as at march"]
            },
            "standalone_cash_flow": {
                "title_kws": ["cash flow", "cashflow", "statement of cash flows"],
                "body_kws": ["operating activities", "investing activities", "financing activities", "cash and cash equivalents", "for the year ended"],
                "not_kws": ["consolidated", "group", "notes to the", "notes forming part", "significant accounting policies", "balance sheet", "earnings per share", "diluted eps"]
            },
            "consolidated_cash_flow": {
                "title_kws": ["cash flow", "cashflow", "statement of cash flows"],
                "body_kws": ["operating activities", "investing activities", "financing activities", "cash and cash equivalents", "consolidated", "for the year ended"],
                "not_kws": ["standalone", "notes to the", "notes forming part", "significant accounting policies", "balance sheet", "earnings per share", "diluted eps"]
            },
            "mda": {
                "title_kws": ["management discussion", "management's discussion", "md&a", "mda"],
                "body_kws": ["indian economy", "industry structure", "opportunities and threats", "internal control systems", "financial performance", "macroeconomic", "risk management", "steel demand", "outlook"],
                "not_kws": ["balance sheet as of", "statement of profit and loss", "cash flow statement", "notes to the financial statements", "notes forming part", "independent auditor's report"]
            },
            "notes_to_accounts": {
                "title_kws": ["notes to the financial statements", "notes to financial statements", "significant accounting policies"],
                "body_kws": ["corporate information", "basis of preparation", "summary of significant accounting policies", "property, plant and equipment", "inventories", "revenue recognition", "employee benefits"],
                "not_kws": ["management discussion", "md&a", "independent auditor's report"]
            },
            "directors_report": {
                "title_kws": ["board's report", "directors' report", "director's report", "letter to shareholders", "message from the ceo"],
                "body_kws": ["financial performance", "dividend", "operations", "directors' responsibility statement", "subsidiaries"],
                "not_kws": ["independent auditor's report", "balance sheet", "profit and loss"]
            },
            "auditors_report": {
                "title_kws": ["independent auditor's report", "auditor's report"],
                "body_kws": ["opinion", "basis for opinion", "key audit matters", "management's responsibility", "auditor's responsibilities"],
                "not_kws": ["board's report", "directors' report", "balance sheet", "profit and loss"]
            }
        }
        
        page_scores = {key: [] for key in missing_keys}
        
        # Collect physical page indices of parsed TOC entries to skip them
        toc_page_indices = {entry["source_page"] - 1 for entry in self.toc_data if "source_page" in entry}
        
        # Identify pages that lack text layer (scanned)
        scanned_pages = []
        page_texts = {}
        for phys_idx in range(0, self.total_pages):
            if phys_idx in toc_page_indices:
                continue
            try:
                page = self.doc[phys_idx]
                text = page.get_text()
                if not text or len(text.strip()) < 50:
                    scanned_pages.append(phys_idx)
                else:
                    page_texts[phys_idx] = text
            except Exception as e:
                logger.error(f"Error reading page {phys_idx}: {e}")
                
        # Perform efficient parallel OCR on scanned pages
        if scanned_pages:
            logger.info(f"Skipping OCR for {len(scanned_pages)} scanned pages as OCR is disabled.")
        
        for phys_idx, text in page_texts.items():
            try:
                text_lower = text.lower()
                text_norm = " ".join(text_lower.split())
                
                for key in missing_keys:
                    if key not in rules:
                        continue
                    
                    kws = rules[key]
                    # Check title keywords (must appear in page text)
                    has_title = any(title_kw in text_norm for title_kw in kws["title_kws"])
                    if not has_title:
                        continue
                    
                    # Score based on body keywords
                    body_score = sum(1.5 for body_kw in kws["body_kws"] if body_kw in text_norm)
                    
                    # Penalty for negative keywords (restrict notes penalty to top of page to avoid footer mentions)
                    not_penalty = 0
                    for not_kw in kws["not_kws"]:
                        if not_kw in ["notes to the", "notes forming part", "significant accounting policies"]:
                            if not_kw in text_norm[:500]:
                                not_penalty += 3.0
                        elif not_kw in text_norm:
                            not_penalty += 3.0
                    
                    # Standalone vs Consolidated header context
                    is_con_cat = "consolidated" in key
                    has_con_header = "consolidated financial statements" in text_norm or "consolidated statement" in text_norm
                    has_stand_header = "standalone financial statements" in text_norm or "standalone statement" in text_norm
                    
                    header_score = 0
                    if is_con_cat:
                        if has_con_header: header_score += 6
                        if has_stand_header: header_score -= 6
                    else:
                        if has_stand_header: header_score += 6
                        if has_con_header: header_score -= 6
                        
                    total_score = body_score - not_penalty + header_score
                    if total_score > 0:
                        page_scores[key].append((phys_idx, total_score))
            except Exception as e:
                logger.error(f"Error scoring page {phys_idx}: {e}")
                
        # Resolve best match for each missing key
        for key in missing_keys:
            if page_scores.get(key):
                # Sort by score descending, then by page index ascending
                sorted_scores = sorted(page_scores[key], key=lambda x: (-x[1], x[0]))
                best_page, best_score = sorted_scores[0]
                results[key] = best_page
                logger.info(f"Fallback matched missing statement '{key}' to physical page {best_page + 1} with score {best_score:.1f}")
                
        return results

    def verify_and_trim_slice(self, key: str, start_phys: int, end_phys: int) -> Tuple[int, int]:
        """
        Verify the slice bounds by reading the text/OCR of the pages.
        Truncates the end_phys if a new statement or notes section starts unexpectedly early.
        """
        if start_phys >= end_phys:
            return start_phys, end_phys
            
        for p in range(start_phys, end_phys):
            if p >= self.total_pages:
                break
                
            page = self.doc[p]
            text = page.get_text()
            
            # Removed OCR fallback
            if len(text.strip()) < 50:
                logger.debug(f"Page {p} has very little text, skipping OCR as it is disabled.")
                    
            text_norm = normalize_text(text)
            
            # 1. Check for Notes to Accounts
            if self.check_notes_start(text):
                # If notes start exactly on the start page, keep 1 page to avoid collapsing to 0
                if p == start_phys:
                    logger.warning(f"Slice verification for {key} found Notes starting exactly on start page {p}! Keeping 1 page fallback.")
                    return start_phys, start_phys + 1
                else:
                    logger.info(f"Slice verification truncated {key} at page {p} due to Notes.")
                    return start_phys, p
            
            # 2. Check for conflicting statement boundaries
            # Only truncate if p > start_phys, to avoid truncating the statement we actually want!
            if p > start_phys:
                if "profit" in key or "pl" in key:
                    if ("balance sheet" in text_norm or "statement of assets" in text_norm) and "profit" not in text_norm[:200]:
                        logger.info(f"Slice verification truncated {key} at page {p} due to Balance Sheet detection.")
                        return start_phys, p
                    if "cash flow" in text_norm or "statement of cash flows" in text_norm:
                        logger.info(f"Slice verification truncated {key} at page {p} due to Cash Flow detection.")
                        return start_phys, p
                
                elif "balance_sheet" in key:
                    if ("profit and loss" in text_norm or "statement of profit" in text_norm) and "balance sheet" not in text_norm[:200]:
                        logger.info(f"Slice verification truncated {key} at page {p} due to P&L detection.")
                        return start_phys, p
                    if "cash flow" in text_norm or "statement of cash flows" in text_norm:
                        logger.info(f"Slice verification truncated {key} at page {p} due to Cash Flow detection.")
                        return start_phys, p
                        
                elif "cash_flow" in key:
                    if ("balance sheet" in text_norm or "statement of assets" in text_norm) and "cash flow" not in text_norm[:200]:
                        logger.info(f"Slice verification truncated {key} at page {p} due to Balance Sheet detection.")
                        return start_phys, p
                    if ("profit and loss" in text_norm or "statement of profit" in text_norm) and "cash flow" not in text_norm[:200]:
                        logger.info(f"Slice verification truncated {key} at page {p} due to P&L detection.")
                        return start_phys, p
        
        return start_phys, end_phys

    def extract_and_zip(self, mappings: Dict[str, Any], output_dir: str) -> Tuple[str, List[str]]:
        """
        Extract the specified page ranges for each statement and package into a ZIP.
        mappings: { statement_key: {"pages": [start_1, end_1], "crop": [x0, y0, x1, y1] | None} }
        returns: (absolute path to the generated ZIP file, list of filenames).
        """
        os.makedirs(output_dir, exist_ok=True)
        extracted_files = []
        
        # Normalize mappings
        normalized_mappings = {}
        for key, value in mappings.items():
            if isinstance(value, dict) and "pages" in value:
                pages = value["pages"]
                crop = value.get("crop")
                if pages and len(pages) >= 2:
                    normalized_mappings[key] = {"start_page": pages[0], "end_page": pages[1], "crop": crop}
                else:
                    normalized_mappings[key] = None
            elif isinstance(value, (list, tuple)) and len(value) >= 2:
                normalized_mappings[key] = {"start_page": value[0], "end_page": value[1], "crop": None}
            else:
                normalized_mappings[key] = None
                
        # Optimization: Pre-calculate if multiple statements start on the same physical page
        start_page_counts = Counter(max(0, p["start_page"] - 1) for k, p in normalized_mappings.items() if k not in ["mda", "directors_report", "auditors_report"] and p and p.get("start_page") and p.get("end_page") and p["start_page"] <= p["end_page"])
        
        # 1. Extract each statement PDF
        master_pdf = fitz.open()
        
        desired_order = [
            "directors_report", 
            "mda", 
            "auditors_report",
            "consolidated_balance_sheet", "consolidated_pl", "consolidated_cash_flow",
            "standalone_balance_sheet", "standalone_pl", "standalone_cash_flow",
            "notes_to_accounts"
        ]
        ordered_keys = [k for k in desired_order if k in normalized_mappings]
        
        for key in ordered_keys:
            page_range = normalized_mappings[key]
            if not page_range or not page_range.get("start_page") or not page_range.get("end_page"):
                continue
            start_1 = page_range["start_page"]
            end_1 = page_range["end_page"]
            user_crop = page_range.get("crop")
            
            # Convert 1-indexed to 0-indexed physical page indices
            start_phys = max(0, start_1 - 1)
            end_phys = min(self.total_pages, end_1)
            
            if start_phys >= end_phys:
                logger.warning(f"Invalid page range for {key}: {start_1} to {end_1}. Skipping.")
                continue
                
            # Perform per-page OCR/text verification to ensure no bleed into adjacent statements
            if key not in ["mda", "directors_report", "auditors_report"]:
                orig_end = end_phys
                start_phys, end_phys = self.verify_and_trim_slice(key, start_phys, end_phys)
                if end_phys != orig_end:
                    logger.info(f"Verified slice for {key}: shrunk from {start_1}-{orig_end} to {start_1}-{end_phys}")
                    
            filename = f"{key}.pdf"
            filepath = os.path.join(output_dir, filename)
            
            try:
                # Same-page slicing logic
                crop_rect = None
                
                # Check for Multiple statements on single page
                if key not in ["mda", "directors_report", "auditors_report"] and start_page_counts[start_phys] > 1:
                    page_to_search = self.doc[start_phys]
                    
                    is_double_page = page_to_search.rect.width > page_to_search.rect.height * 1.2
                    if is_double_page:
                        logger.info(f"Multiple statements detected on physical page {start_phys} (Double Spread). Attempting vertical split for {key}.")
                        # Figure out if it's on left or right half
                        search_title = []
                        if "balance_sheet" in key: search_title = ["Balance Sheet", "Assets and Liabilities"]
                        elif "pl" in key or "profit" in key: search_title = ["Profit and Loss", "Statement of Profit", "Income Statement"]
                        elif "cash_flow" in key: search_title = ["Cash Flow"]
                        
                        found_x = None
                        for kw in search_title:
                            rects = page_to_search.search_for(kw)
                            if rects:
                                found_x = rects[0].x0
                                break
                                
                        if found_x is not None:
                            mid_x = page_to_search.rect.width / 2
                            if found_x < mid_x:
                                crop_rect = fitz.Rect(0, 0, mid_x, page_to_search.rect.height)
                                logger.info(f"Found '{key}' on left half (X={found_x}).")
                            else:
                                crop_rect = fitz.Rect(mid_x, 0, page_to_search.rect.width, page_to_search.rect.height)
                                logger.info(f"Found '{key}' on right half (X={found_x}).")
                        else:
                            logger.warning(f"Could not find title X-coordinate for {key}, using full spread.")
                    else:
                        logger.info(f"Multiple statements detected on physical page {start_phys} (Single Page). Attempting horizontal split for {key}.")
                        # Search for the next logical statement title on this page
                        next_title_kws = []
                        if "balance_sheet" in key:
                            next_title_kws = ["Profit and Loss", "Statement of Profit"]
                        elif "pl" in key or "profit" in key:
                            next_title_kws = ["Cash Flow", "Statement of Cash"]
                        
                        split_y = None
                        for kw in next_title_kws:
                            rects = page_to_search.search_for(kw)
                            if rects:
                                # Use the top of the found text as the split line
                                split_y = rects[0].y0
                                logger.info(f"Found '{kw}' on page {start_phys} at Y={split_y}. Slicing page horizontally.")
                                break
                                
                        if split_y is not None:
                            # If we are extracting the FIRST statement (e.g. Balance Sheet), we take top half
                            if "balance_sheet" in key:
                                crop_rect = fitz.Rect(0, 0, page_to_search.rect.width, split_y)
                            # If we are extracting the SECOND statement (e.g. P&L), we take bottom half
                            elif "pl" in key or "profit" in key:
                                crop_rect = fitz.Rect(0, split_y - 10, page_to_search.rect.width, page_to_search.rect.height)
                
                # Slicing using PyMuPDF (fitz)
                if crop_rect and start_phys < end_phys:
                    temp_doc = fitz.open()
                    temp_doc.insert_pdf(self.doc, from_page=start_phys, to_page=start_phys)
                    temp_doc[0].set_cropbox(crop_rect)
                    if end_phys - 1 > start_phys:
                        temp_doc.insert_pdf(self.doc, from_page=start_phys + 1, to_page=end_phys - 1)
                    master_pdf.insert_pdf(temp_doc)
                    temp_doc.close()
                else:
                    master_pdf.insert_pdf(self.doc, from_page=start_phys, to_page=end_phys - 1)
                    
                logger.info(f"Appended {key} (pages {start_1}-{end_1}) to master trimmed PDF")
                
            except Exception as e:
                logger.error(f"Failed to extract {key}: {e}", exc_info=True)
                
        # Save master PDF
        master_pdf_path = os.path.join(output_dir, "trimmed_annual_report.pdf")
        master_pdf.save(master_pdf_path)
        master_pdf.close()
        extracted_files.append(("trimmed_annual_report.pdf", master_pdf_path))
                
        # 2. Bundle into a single ZIP file
        zip_path = os.path.join(output_dir, "financial_statements.zip")
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for filename, filepath in extracted_files:
                    zipf.write(filepath, arcname=filename)
            logger.info(f"ZIP package created successfully at {zip_path}")
        except Exception as e:
            logger.error(f"Failed to create ZIP archive: {e}", exc_info=True)
            raise e
            
        return zip_path, [f[0] for f in extracted_files]

