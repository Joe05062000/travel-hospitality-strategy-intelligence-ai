"""Executive PDF with wrapped evidence, source links and page numbers."""
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

from .sectors import BRAND


def report_pdf(title, lines, citations, *, sections=()):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    target = BytesIO()
    styles = getSampleStyleSheet()
    styles["Title"].textColor = colors.HexColor("#123352")
    body = styles["BodyText"]
    body.spaceAfter = 8
    body.leading = 14
    story = [Paragraph(escape(BRAND.upper()), styles["Heading3"]),
             Paragraph(escape(title), styles["Title"]), Spacer(1, 14)]
    story.extend(Paragraph(escape(line), body) for line in lines)
    for heading, paragraphs in sections:
        if paragraphs:
            story.append(Paragraph(escape(heading), styles["Heading2"]))
            story.extend(Paragraph(escape(line), body) for line in paragraphs)
    if citations:
        story.append(Paragraph("Evidence sources", styles["Heading2"]))
    for number, url in enumerate(citations, 1):
        story.append(Paragraph(f'[{number}] <link color="#145994" href={quoteattr(url)}>'
                               f'{escape(url)}</link>', body))

    def footer(canvas, document):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#49647D"))
        canvas.drawString(document.leftMargin, 38, BRAND)
        canvas.drawRightString(document.pagesize[0] - document.rightMargin, 38,
                               str(document.page))

    SimpleDocTemplate(target, title=title, author=BRAND).build(
        story, onFirstPage=footer, onLaterPages=footer)
    return target.getvalue()
