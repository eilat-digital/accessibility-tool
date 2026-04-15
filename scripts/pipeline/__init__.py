"""
scripts/pipeline — PDF/UA-1 accessibility pipeline for Eilat Municipality.

Modules
-------
models      TextBlock, StructElement, ValidationResult
classifier  DocumentClassifier, DocumentType, DOC_TYPE_LABELS, type_specific_warnings
parser      extract_blocks()  — pdfminer text extraction with layout info
detector    StructureDetector, HeadingDetector, TableDetector, sort_reading_order
            merge_ai_structure()
tag_builder inject_digital(), inject_scanned(), build_bookmarks()
validator   StructValidator, FileValidator

Pipeline flow
-------------
  blocks = extract_blocks(pdf)
  lines  = extract_lines(pdf)
  doc_type = DocumentClassifier().classify(blocks)
  elements = StructureDetector().detect(blocks, graphic_lines=lines, doc_type=doc_type)
  warnings = type_specific_warnings(elements, doc_type)
  inject_digital(pdf, elements, ...)
"""

from .models      import TextBlock, StructElement, ValidationResult
from .classifier  import DocumentClassifier, DocumentType, DOC_TYPE_LABELS, type_specific_warnings
from .parser      import extract_blocks, extract_lines, has_text
from .parser      import GraphicLine
from .detector    import (
    StructureDetector,
    HeadingDetector,
    TableDetector,
    BorderTableDetector,
    sort_reading_order,
    merge_ai_structure,
)
from .tag_builder import inject_digital, inject_scanned, inject_scanned_semantic, build_bookmarks
from .validator          import StructValidator, FileValidator
from .semantic_validator import (
    SemanticValidator, GateResult, PACGate, PACResult, Finding,
)

__all__ = [
    # models
    "TextBlock", "StructElement", "ValidationResult",
    # classifier
    "DocumentClassifier", "DocumentType", "DOC_TYPE_LABELS", "type_specific_warnings",
    # parser
    "extract_blocks", "extract_lines", "has_text", "GraphicLine",
    # detector
    "StructureDetector", "HeadingDetector", "TableDetector",
    "BorderTableDetector", "sort_reading_order", "merge_ai_structure",
    # tag_builder
    "inject_digital", "inject_scanned", "inject_scanned_semantic", "build_bookmarks",
    # validator (pre-export)
    "StructValidator", "FileValidator",
    # semantic gate (hard-fail layer)
    "SemanticValidator", "GateResult", "PACGate", "PACResult", "Finding",
]
