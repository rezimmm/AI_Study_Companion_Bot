import PyPDF2

def extract_pdf_text(file_path):
    reader = PyPDF2.PdfReader(open(file_path, "rb"))
    text = ""
    for page in reader.pages:
        text += page.extract_text()
    return text
