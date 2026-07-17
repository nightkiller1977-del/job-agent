import ast
import os
from pathlib import Path
from src.blocker_classifier import classify, BlockerClass

def test_no_unknown_statuses_emitted():
    """
    Exhaustively parse all Python source files for emitted statuses and ensure
    none of them map to BlockerClass.UNKNOWN. This prevents vocabulary drift.
    """
    src_dir = Path(__file__).parent.parent / "src"
    emitted_statuses = set()

    for root, _, files in os.walk(src_dir):
        for file in files:
            if not file.endswith(".py"):
                continue
            
            filepath = Path(root) / file
            try:
                tree = ast.parse(filepath.read_text())
            except Exception:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    # Check for record_apply_attempt(..., status="...")
                    is_record = False
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "record_apply_attempt":
                        is_record = True
                    elif isinstance(node.func, ast.Name) and node.func.id == "record_apply_attempt":
                        is_record = True
                    
                    if is_record:
                        # Extract the status argument (it's the second positional argument)
                        # def record_apply_attempt(self, job_id: str, status: str, ...)
                        if len(node.args) >= 2:
                            status_arg = node.args[1]
                            if isinstance(status_arg, ast.Constant) and isinstance(status_arg.value, str):
                                emitted_statuses.add(status_arg.value)
                        
                        # Also check keyword arguments just in case
                        for kw in node.keywords:
                            if kw.arg == "status" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                emitted_statuses.add(kw.value.value)

    # Some statuses are dynamically generated like {vendor}_submit_not_found
    # Since AST only catches string literals, this checks all explicit string literals.
    # We also manually add known dynamic ones to ensure families are caught.
    emitted_statuses.update([
        "ashby_submit_not_found",
        "workday_form_not_reached",
        "microsoft_login_required",
        "linkedin_step_blocked",
        "unknown_source_form_not_detected"
    ])

    unknowns = [s for s in emitted_statuses if classify(s) == BlockerClass.UNKNOWN]
    
    assert not unknowns, f"Found statuses mapping to UNKNOWN: {unknowns}"
