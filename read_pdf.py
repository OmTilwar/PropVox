import subprocess
import sys

def install_and_import():
    try:
        import PyPDF2
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "PyPDF2"])
        import PyPDF2

install_and_import()

import PyPDF2

def read_pdf(file_path):
    text = ""
    with open(file_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        for page in reader.pages:
            if page.extract_text():
                text += page.extract_text() + "\n"
    return text

if __name__ == "__main__":
    content = read_pdf(sys.argv[1])
    with open(sys.argv[2], 'w', encoding='utf-8') as f:
        f.write(content)
