"""Step 1 of the pipeline: classify the raw input so the resolver can pick a skill.
Equivalent to gbrain's 'signal-detector' skill, simplified to file-extension + content sniff."""
import os


def classify(filepath: str) -> str:
    """Returns one of: 'pdf', 'transcript', 'txt'.

    - .pdf                          -> 'pdf'
    - .txt/.md with speaker markers -> 'transcript'  (e.g. "Sarah: ..." lines)
    - anything else text-based      -> 'txt'
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".pdf":
        return "pdf"

    if ext in (".txt", ".md"):
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            sample = f.read(2000)
        if _looks_like_transcript(sample):
            return "transcript"
        return "txt"

    raise ValueError(f"Unsupported file type: {ext}. Supported: .pdf, .txt, .md")


def _looks_like_transcript(text: str) -> bool:
    """Heuristic: transcripts have repeated 'Name:' style speaker turns."""
    import re
    speaker_lines = re.findall(r"^[A-Z][a-zA-Z .]{1,30}:\s", text, re.MULTILINE)
    return len(speaker_lines) >= 3
