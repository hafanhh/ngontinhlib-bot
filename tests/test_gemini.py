"""Phase 2 test: ask the 4 sample questions in the terminal (no Telegram needed).
Run with:  python -m tests.test_gemini
Or ask your own question:  python -m tests.test_gemini "Tìm truyện hài hước đã hoàn"
"""

import sys

from services.gemini_agent import answer_question

SAMPLE_QUESTIONS = [
    "Tìm truyện sủng, HE, chưa đọc, dưới 100 chương",
    "Gợi ý truyện cổ đại cung đấu đã hoàn",
    "Truyện nào của tác giả Lựu Cảnh?",
    "Truyện nào mình đã đọc xong mà ngọt nhẹ nhàng?",
]

# Use the question from the command line if given, otherwise the 4 samples
questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else SAMPLE_QUESTIONS

for number, q in enumerate(questions):
    print(f"\n=== Hỏi: {q} ===")
    # A different chat number per question, so each one starts a fresh conversation
    print(answer_question(number, q))
