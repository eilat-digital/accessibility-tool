"""
scripts/pipeline — PDF/UA-1 accessibility pipeline for Eilat Municipality.

Modules
-------
models      TextBlock, StructElement, ValidationResult
parser      extract_blocks()  — pdfminer text extraction with layout info
detector    StructureDetector, HeadingDetector, TableDetector, sort_reading_order
            merge_ai_structure()
tag_builder inject_digital(), inject_scanned(), build_bookmarks()
validator   StructValidator, FileValidator
"""

from .models    import TextBlock, StructElement, ValidationResult
from .parser    import extract_blocks, extract_lines, has_text
from .parser    import GraphicLine
from .detector  import (
    StructureDetector,
    HeadingDetector,
    TableDetector,
    BorderTableDetector,
    sort_reading_order,
    merge_ai_structure,
)
from .tag_builder import inject_digital, inject_scanned, build_bookmarks
from .validator   import StructValidator, FileValidator

__all__ = [
    # models
    "TextBlock", "StructElement", "ValidationResult",
    # parser
    "extract_blocks", "extract_lines", "has_text", "GraphicLine",
    # detector
    "StructureDetector", "HeadingDetector", "TableDetector",
    "BorderTableDetector", "sort_reading_order", "merge_ai_structure",
    # tag_builder
    "inject_digital", "inject_scanned", "build_bookmarks",
    # validator
    "StructValidator", "FileValidator",
]
