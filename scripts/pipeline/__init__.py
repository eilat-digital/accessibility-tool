"""
scripts/pipeline — PDF/UA-1 accessibility pipeline for Eilat Municipality.

Modules in this PR
------------------
models      TextBlock, StructElement, ValidationResult
classifier  DocumentClassifier, DocumentType, DOC_TYPE_LABELS, type_specific_warnings
detector    StructureDetector, HeadingDetector, TableDetector, BorderTableDetector,
            sort_reading_order, merge_ai_structure
"""

from .models     import TextBlock, StructElement, ValidationResult
from .classifier import DocumentClassifier, DocumentType, DOC_TYPE_LABELS, type_specific_warnings
from .detector   import (
    StructureDetector,
    HeadingDetector,
    TableDetector,
    BorderTableDetector,
    sort_reading_order,
    merge_ai_structure,
)

__all__ = [
    # models
    "TextBlock", "StructElement", "ValidationResult",
    # classifier
    "DocumentClassifier", "DocumentType", "DOC_TYPE_LABELS", "type_specific_warnings",
    # detector
    "StructureDetector", "HeadingDetector", "TableDetector",
    "BorderTableDetector", "sort_reading_order", "merge_ai_structure",
]
