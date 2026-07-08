from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from playwright.async_api import Page
from rich.console import Console

console = Console()

def escape_latex(text: str) -> str:
    """Escapes special LaTeX characters in a text string to prevent compilation failures."""
    if not isinstance(text, str):
        return ""
    latex_special = {
        '&': r'\&',
        '%': r'\%',
        '$': r'\$',
        '#': r'\#',
        '_': r'\_',
        '{': r'\{',
        '}': r'\}',
        '~': r'\textasciitilde{}',
        '^': r'\textasciicircum{}',
        '\\': r'\textbackslash{}',
    }
    # Escape backslash first
    text = text.replace('\\', latex_special['\\'])
    for char, escaped in latex_special.items():
        if char != '\\':
            text = text.replace(char, escaped)
    return text

class LaTeXCompiler:
    def __init__(self, project_root: str | Path | None = None):
        if project_root is None:
            self.project_root = Path(__file__).parent.parent
        else:
            self.project_root = Path(project_root)
        self.cv_template_path = self.project_root / "cv" / "cv_template.tex"
        self.cover_template_path = self.project_root / "cover_letters" / "cover_template.tex"

    def has_latex_compiler(self) -> str | None:
        """Checks if a LaTeX compiler is available on the system PATH.
        Returns the command name if found, or None.
        """
        for cmd in ["lualatex", "xelatex", "pdflatex"]:
            if shutil.which(cmd):
                return cmd
        return None

    def _compile_with_latex(self, tex_content: str, output_pdf_path: str, compiler_cmd: str) -> bool:
        """Saves tex_content to a temp file, runs the compiler, and copies the PDF to output_pdf_path."""
        temp_dir = tempfile.mkdtemp()
        try:
            tex_file = Path(temp_dir) / "document.tex"
            tex_file.write_text(tex_content, encoding="utf-8")

            # Run the LaTeX compiler twice for resolving page numbers/references if needed
            for _ in range(2):
                result = subprocess.run(
                    [compiler_cmd, "-interaction=nonstopmode", "-output-directory", temp_dir, str(tex_file)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30
                )
                if result.returncode != 0:
                    console.print(f"[red]LaTeX compilation error ({compiler_cmd}):[/red]\n{result.stdout}")
                    return False

            generated_pdf = Path(temp_dir) / "document.pdf"
            if generated_pdf.exists():
                os.makedirs(os.path.dirname(output_pdf_path), exist_ok=True)
                shutil.copy(generated_pdf, output_pdf_path)
                return True
            return False
        except Exception as e:
            console.print(f"[red]LaTeX compilation exception:[/red] {e}")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def compile_cv(self, profile: dict, tailored: dict, output_pdf_path: str, page: Page | None = None) -> bool:
        """Compiles a tailored CV to PDF. Uses LaTeX if available, otherwise falls back to HTML-to-PDF."""
        compiler = self.has_latex_compiler()
        if compiler:
            console.print(f"[green]LaTeX compiler '{compiler}' found.[/green] Generating LaTeX CV...")
            return self._compile_cv_latex(profile, tailored, output_pdf_path, compiler)
        else:
            console.print("[yellow]No LaTeX compiler found on system.[/yellow] Falling back to HTML-to-PDF via Playwright...")
            if not page:
                raise ValueError("Playwright Page object is required for HTML-to-PDF fallback compilation.")
            return await self._compile_cv_html(profile, tailored, output_pdf_path, page)

    async def compile_cover_letter(self, profile: dict, tailored: dict, job: dict, output_pdf_path: str, page: Page | None = None) -> bool:
        """Compiles a tailored cover letter to PDF. Uses LaTeX if available, otherwise falls back to HTML-to-PDF."""
        compiler = self.has_latex_compiler()
        if compiler:
            console.print(f"[green]LaTeX compiler '{compiler}' found.[/green] Generating LaTeX Cover Letter...")
            return self._compile_cover_letter_latex(profile, tailored, job, output_pdf_path, compiler)
        else:
            console.print("[yellow]No LaTeX compiler found on system.[/yellow] Falling back to HTML-to-PDF via Playwright...")
            if not page:
                raise ValueError("Playwright Page object is required for HTML-to-PDF fallback compilation.")
            return await self._compile_cover_letter_html(profile, tailored, job, output_pdf_path, page)

    def _compile_cv_latex(self, profile: dict, tailored: dict, output_pdf_path: str, compiler_cmd: str) -> bool:
        if not self.cv_template_path.exists():
            console.print(f"[red]CV Template file not found at {self.cv_template_path}[/red]")
            return False

        template_content = self.cv_template_path.read_text(encoding="utf-8")

        info = profile.get("personal_info", {})
        first_name = info.get("first_name", "")
        last_name = info.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or "Candidate"

        email = info.get("email", "")
        phone = info.get("phone", "")
        linkedin = profile.get("social_links", {}).get("linkedin", "")
        city = info.get("city", "")
        state_abbr = info.get("state", "")
        location = ", ".join(p for p in [city, state_abbr] if p)
        contact_info = " | ".join(p for p in [email, phone, location, linkedin] if p)

        summary = tailored.get("tailored_summary", "")

        # Format experience
        work_history_tex = ""
        work_history = profile.get("work_history", [])
        tailored_bullets = tailored.get("tailored_bullets", [])
        for i, role in enumerate(tailored_bullets):
            wh = work_history[i] if i < len(work_history) else {}
            company = escape_latex(wh.get("company_name", ""))
            title = escape_latex(wh.get("job_title", role.get("role", "")))
            start = escape_latex(wh.get("start_date", ""))
            end = escape_latex(wh.get("end_date", "Present"))

            bullets = role.get("bullets", [])
            bullets_tex = "\n".join(f"    \\item {escape_latex(b)}" for b in bullets)

            work_history_tex += (
                f"\\noindent \\textbf{{{title}}} \\hfill {{{start} -- {end}}} \\\\\n"
                f"\\noindent \\textit{{{company}}} \\\\\n"
                f"\\begin{{itemize}}\n"
                f"{bullets_tex}\n"
                f"\\end{{itemize}}\n"
                f"\\vspace{{8pt}}\n"
            )

        # Format education
        education_tex = ""
        for ed in profile.get("education", []):
            school = escape_latex(ed.get("school_name", ""))
            degree = escape_latex(ed.get("degree", ""))
            major = escape_latex(ed.get("major", ""))
            start = escape_latex(ed.get("start_date", ""))
            end = escape_latex(ed.get("end_date", ""))
            education_tex += (
                f"\\noindent \\textbf{{{degree} in {major}}} \\hfill {{{start} -- {end}}} \\\\\n"
                f"\\noindent \\textit{{{school}}} \\\\\n"
                f"\\vspace{{6pt}}\n"
            )

        # Format skills
        skills_list = list(profile.get("skills", []))
        for kw in tailored.get("missing_keywords", []):
            if kw and kw not in skills_list:
                skills_list.append(kw)
        skills_tex = ", ".join(escape_latex(s) for s in skills_list)

        content = template_content
        content = content.replace("{{FULL_NAME}}", escape_latex(full_name))
        content = content.replace("{{CONTACT_INFO}}", escape_latex(contact_info))
        content = content.replace("{{SUMMARY}}", escape_latex(summary))
        content = content.replace("{{WORK_HISTORY}}", work_history_tex)
        content = content.replace("{{EDUCATION}}", education_tex)
        content = content.replace("{{SKILLS}}", skills_tex)

        return self._compile_with_latex(content, output_pdf_path, compiler_cmd)

    def _compile_cover_letter_latex(self, profile: dict, tailored: dict, job: dict, output_pdf_path: str, compiler_cmd: str) -> bool:
        if not self.cover_template_path.exists():
            console.print(f"[red]Cover Letter Template file not found at {self.cover_template_path}[/red]")
            return False

        template_content = self.cover_template_path.read_text(encoding="utf-8")

        info = profile.get("personal_info", {})
        first_name = info.get("first_name", "")
        last_name = info.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or "Candidate"

        email = info.get("email", "")
        phone = info.get("phone", "")
        city = info.get("city", "")
        state_abbr = info.get("state", "")
        location = ", ".join(p for p in [city, state_abbr] if p)
        contact_info = " | ".join(p for p in [email, phone, location] if p)

        company_name = job.get("company", "Hiring Organization")
        company_address = job.get("company_address", "Company Headquarters")
        job_title = job.get("title", "Software Engineering Position")
        job_id = job.get("job_id") or job.get("id", "N/A")

        raw_cover_body = tailored.get("cover_letter", "")
        paragraphs = [escape_latex(p.strip()) for p in raw_cover_body.split("\n\n") if p.strip()]
        cover_body_tex = "\n\n".join(paragraphs)

        content = template_content
        content = content.replace("{{FULL_NAME}}", escape_latex(full_name))
        content = content.replace("{{CONTACT_INFO}}", escape_latex(contact_info))
        content = content.replace("{{COMPANY_NAME}}", escape_latex(company_name))
        content = content.replace("{{COMPANY_ADDRESS}}", escape_latex(company_address))
        content = content.replace("{{JOB_TITLE}}", escape_latex(job_title))
        content = content.replace("{{JOB_ID}}", escape_latex(str(job_id)))
        content = content.replace("{{COVER_LETTER_BODY}}", cover_body_tex)

        return self._compile_with_latex(content, output_pdf_path, compiler_cmd)

    async def _compile_cv_html(self, profile: dict, tailored: dict, output_pdf_path: str, page: Page) -> bool:
        try:
            info = profile.get("personal_info", {})
            name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or "Candidate"
            email = info.get("email", "")
            phone = info.get("phone", "")
            linkedin = profile.get("social_links", {}).get("linkedin", "")
            city = info.get("city", "")
            state_abbr = info.get("state", "")
            location = ", ".join(p for p in [city, state_abbr] if p)

            skills = list(profile.get("skills", []))
            for kw in tailored.get("missing_keywords", []):
                if kw and kw not in skills:
                    skills.append(kw)

            education = profile.get("education", [])
            work_history = profile.get("work_history", [])
            tailored_bullets = tailored.get("tailored_bullets", [])

            experience_html = ""
            for i, role in enumerate(tailored_bullets):
                wh = work_history[i] if i < len(work_history) else {}
                company = wh.get("company_name", "")
                title = wh.get("job_title", role.get("role", ""))
                start = wh.get("start_date", "")
                end = wh.get("end_date", "Present")
                bullets_html = "".join(f"<li>{b}</li>" for b in role.get("bullets", []))
                experience_html += (
                    f'<div class="exp-item">'
                    f'<div class="exp-header"><span class="exp-title">{title}</span>'
                    f'<span class="exp-dates">{start} – {end}</span></div>'
                    f'<div class="exp-company">{company}</div>'
                    f"<ul>{bullets_html}</ul></div>"
                )

            edu_html = ""
            for ed in education:
                edu_html += (
                    f'<div class="exp-item">'
                    f'<div class="exp-header"><span class="exp-title">{ed.get("degree","")} — {ed.get("major","")}</span>'
                    f'<span class="exp-dates">{ed.get("start_date","")} – {ed.get("end_date","")}</span></div>'
                    f'<div class="exp-company">{ed.get("school_name","")}</div></div>'
                )

            summary = tailored.get("tailored_summary", "")
            skills_html = " &bull; ".join(skills)
            contact_html = " | ".join(p for p in [email, phone, location, linkedin] if p)

            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>"
                "body{font-family:Arial,sans-serif;font-size:11pt;color:#222;margin:0;padding:20px 30px}"
                "h1{font-size:20pt;margin:0 0 2px;color:#1e293b}"
                ".contact{font-size:9pt;color:#555;margin-bottom:10px}"
                "h2{font-size:12pt;border-bottom:1px solid #999;padding-bottom:2px;"
                "margin:12px 0 6px;text-transform:uppercase;letter-spacing:.05em;color:#1e293b}"
                ".summary,.skills{margin-bottom:8px;line-height:1.5}"
                ".exp-item{margin-bottom:10px}"
                ".exp-header{display:flex;justify-content:space-between;font-weight:bold}"
                ".exp-dates{font-weight:normal;font-size:10pt;color:#555}"
                ".exp-company{font-style:italic;font-size:10pt;margin-bottom:3px}"
                "ul{margin:4px 0 0 18px;padding:0}li{margin-bottom:2px;line-height:1.4}"
                "</style></head><body>"
                f"<h1>{name}</h1>"
                f"<div class='contact'>{contact_html}</div>"
                "<h2>Professional Summary</h2>"
                f"<div class='summary'>{summary}</div>"
                "<h2>Core Competencies</h2>"
                f"<div class='skills'>{skills_html}</div>"
                "<h2>Professional Experience</h2>"
                f"{experience_html}"
                "<h2>Education</h2>"
                f"{edu_html}"
                "</body></html>"
            )

            os.makedirs(os.path.dirname(output_pdf_path), exist_ok=True)
            _pdf_page = await page.context.new_page()
            try:
                await _pdf_page.set_content(html, wait_until="domcontentloaded")
                await _pdf_page.pdf(
                    path=output_pdf_path,
                    format="Letter",
                    margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                )
                console.print(f"[green]HTML Fallback CV generated:[/green] {output_pdf_path}")
                return True
            finally:
                await _pdf_page.close()
        except Exception as e:
            console.print(f"[red]HTML-to-PDF CV fallback generation failed:[/red] {e}")
            return False

    async def _compile_cover_letter_html(self, profile: dict, tailored: dict, job: dict, output_pdf_path: str, page: Page) -> bool:
        try:
            info = profile.get("personal_info", {})
            name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or "Candidate"
            email = info.get("email", "")
            phone = info.get("phone", "")
            city = info.get("city", "")
            state_abbr = info.get("state", "")
            location = ", ".join(p for p in [city, state_abbr] if p)
            contact_html = " | ".join(p for p in [email, phone, location] if p)

            company_name = job.get("company", "Hiring Organization")
            job_title = job.get("title", "Software Engineering Position")
            job_id = job.get("job_id") or job.get("id", "N/A")

            raw_cover_body = tailored.get("cover_letter", "")
            paragraphs = [f"<p>{p.strip()}</p>" for p in raw_cover_body.split("\n\n") if p.strip()]
            cover_body_html = "\n".join(paragraphs)

            import datetime
            date_str = datetime.date.today().strftime("%B %d, %Y")

            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>"
                "body{font-family:Arial,sans-serif;font-size:11pt;color:#222;margin:0;padding:40px 50px;line-height:1.6}"
                ".sender-info{text-align:right;margin-bottom:30px;color:#555;font-size:10pt}"
                ".sender-name{font-weight:bold;font-size:12pt;color:#1e293b}"
                ".date{margin-bottom:20px}"
                ".recipient{margin-bottom:30px;font-weight:bold}"
                ".subject{font-weight:bold;margin-bottom:20px;font-size:11pt;color:#1e293b;border-bottom:1px solid #ddd;padding-bottom:5px}"
                "p{margin-bottom:15px;text-align:justify}"
                ".signature{margin-top:40px}"
                "</style></head><body>"
                f"<div class='sender-info'>"
                f"<span class='sender-name'>{name}</span><br>"
                f"{contact_html}<br>"
                f"</div>"
                f"<div class='date'>{date_str}</div>"
                f"<div class='recipient'>"
                f"To the Hiring Team<br>"
                f"{company_name}"
                f"</div>"
                f"<div class='subject'>Subject: Application for {job_title} (Job ID: {job_id})</div>"
                f"<div class='body'>{cover_body_html}</div>"
                f"<div class='signature'>"
                f"Sincerely,<br><br><br>"
                f"<strong>{name}</strong>"
                f"</div>"
                "</body></html>"
            )

            os.makedirs(os.path.dirname(output_pdf_path), exist_ok=True)
            _pdf_page = await page.context.new_page()
            try:
                await _pdf_page.set_content(html, wait_until="domcontentloaded")
                await _pdf_page.pdf(
                    path=output_pdf_path,
                    format="Letter",
                    margin={"top": "1.0in", "bottom": "1.0in", "left": "1.0in", "right": "1.0in"},
                )
                console.print(f"[green]HTML Fallback Cover Letter generated:[/green] {output_pdf_path}")
                return True
            finally:
                await _pdf_page.close()
        except Exception as e:
            console.print(f"[red]HTML-to-PDF Cover Letter fallback generation failed:[/red] {e}")
            return False
