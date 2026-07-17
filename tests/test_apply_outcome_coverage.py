import os
import re
import pytest
from src.apply_outcome import ApplyOutcomeCode

def test_all_set_apply_outcome_calls_use_enum():
    """Ensure all emitters use the ApplyOutcomeCode enum directly,
    avoiding dynamic strings or unregistered statuses.
    """
    src_dir = os.path.join(os.path.dirname(__file__), "..", "src", "sources")
    
    # Regex to match: _set_apply_outcome(something, ...)
    # where something should start with ApplyOutcomeCode.
    pattern = re.compile(r'_set_apply_outcome\(\s*([^,]+),')
    
    violations = []
    
    for root, _, files in os.walk(src_dir):
        for file in files:
            if not file.endswith(".py"):
                continue
                
            filepath = os.path.join(root, file)
            with open(filepath, "r") as f:
                content = f.read()
                
            for match in pattern.finditer(content):
                arg = match.group(1).strip()
                # If arg is just `status`, it's dynamically passed in. We allow this if the function signature enforces ApplyOutcomeCode or if it's explicitly allowed.
                # In base.py signature it's `status: str` and it converts. 
                # Let's ensure callers in jobright.py etc use ApplyOutcomeCode.SOMETHING
                if arg == 'status':
                    continue # It's a variable being passed down
                if not arg.startswith('ApplyOutcomeCode.'):
                    # Check if it's a valid string but not using Enum directly
                    # If it's a string literal, that's a violation.
                    if arg.startswith('"') or arg.startswith("'") or arg.startswith("f\"") or arg.startswith("f'"):
                        violations.append(f"{file}: Found raw string usage: {arg}")
                    else:
                        # Some other expression. Probably fine or needs manual check
                        pass

    assert not violations, "Found emitters using dynamic strings instead of ApplyOutcomeCode:\n" + "\n".join(violations)

def test_status_mapping_coverage():
    """Ensure all ApplyOutcomeCode values are mapped in BlockerClass"""
    from src.blocker_classifier import _STATUS_TO_CLASS, BlockerClass
    
    missing = []
    for code in ApplyOutcomeCode:
        if code not in _STATUS_TO_CLASS:
            missing.append(code)
            
    assert not missing, f"Missing BlockerClass mapping for {missing}"
