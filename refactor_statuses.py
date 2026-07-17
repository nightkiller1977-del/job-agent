import os
import re

def process_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Add imports
    if 'ApplyOutcomeCode' not in content and '_set_apply_outcome' in content:
        content = "from src.apply_outcome import ApplyOutcomeCode\n" + content

    # Replace f"{family}_something" with ApplyOutcomeCode.SOMETHING, portal_family=family
    def repl_fstring(m):
        prefix = m.group(1) # e.g. self. or return self.
        family_var = m.group(2)
        status_name = m.group(3).upper()
        # map apply_not_reached to FORM_NOT_REACHED for consistency
        if status_name == 'APPLY_NOT_REACHED':
            status_name = 'FORM_NOT_REACHED'
        rest = m.group(4)
        return f"{prefix}_set_apply_outcome(ApplyOutcomeCode.{status_name}, {rest}, portal_family={family_var})"

    content = re.sub(r'(\w*\s*self\.)_set_apply_outcome\(\s*f"\{([^}]+)\}_([^"]+)"\s*,\s*(.*?)\)', repl_fstring, content, flags=re.DOTALL)

    # Replace f"{family}_submit_not_found" if family != "generic" else "submit_not_found"
    # Actually just simplify this one manually
    content = content.replace(
        'f"{family}_submit_not_found" if family != "generic" else "submit_not_found"',
        'ApplyOutcomeCode.SUBMIT_NOT_FOUND, portal_family=family if family != "generic" else ""'
    )

    # Replace string literals "something" with ApplyOutcomeCode.SOMETHING
    def repl_literal(m):
        prefix = m.group(1)
        status_name = m.group(2).upper()
        rest = m.group(3)
        return f"{prefix}_set_apply_outcome(ApplyOutcomeCode.{status_name}, {rest})"

    # We need to make sure we don't catch things that aren't strings, so \"([a-z_]+)\"
    content = re.sub(r'(\w*\s*self\.)_set_apply_outcome\(\s*"([a-z_]+)"\s*,\s*(.*?)\)', repl_literal, content, flags=re.DOTALL)

    with open(filepath, 'w') as f:
        f.write(content)

for root, _, files in os.walk('src/sources'):
    for file in files:
        if file.endswith('.py') and file != 'base.py':
            process_file(os.path.join(root, file))
