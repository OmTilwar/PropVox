import zipfile
import xml.etree.ElementTree as ET
import sys

def read_docx(file_path):
    try:
        with zipfile.ZipFile(file_path) as docx:
            tree = ET.XML(docx.read('word/document.xml'))
            text = []
            for paragraph in tree.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                para_text = "".join([node.text for node in paragraph.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t') if node.text])
                if para_text:
                    text.append(para_text)
            return "\n".join(text)
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    print(read_docx(sys.argv[1]))
