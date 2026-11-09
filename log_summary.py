"""Print a short summary of today's questions from logs/bot.log.

Run:  python log_summary.py            (today)
      python log_summary.py 2026-11-09 (another day)

It only READS the log. It looks at three kinds of lines the bot writes for each question:
  "... [k3x9a] Incoming: ..."        -> counts a question
  "... [k3x9a] usage: ... prompt=3203 output=163 ..." -> adds up tokens
  "... [k3x9a] Done: status=ok total_time=2.31s"  -> response time and errors
"""

import glob
import os
import re
import sys
from datetime import date

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "bot.log")

# Pieces of a log line we look for
RE_INCOMING = re.compile(r"\] Incoming:")
RE_USAGE = re.compile(r"\] usage: .*")
RE_DONE = re.compile(r"\] Done: status=(\w+) total_time=([\d.]+)s")


def number_after(name: str, line: str):
    """Find 'name=123' in a line. Returns the number, or None (for 'n/a' or missing)."""
    match = re.search(rf"\b{name}=(\d+)", line)
    return int(match.group(1)) if match else None


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    # bot.log plus the older copies (bot.log.1, .2, .3), in case the log rotated today
    files = sorted(glob.glob(LOG_FILE + "*"))
    if not files:
        raise SystemExit(f"Không tìm thấy {LOG_FILE}. Hãy chạy bot và hỏi vài câu trước.")

    questions = errors = 0
    prompt_tokens = output_tokens = 0
    times = []

    for path in files:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith(day):  # lines start with the date: 2026-11-09 16:55:50,094
                    continue
                if RE_INCOMING.search(line):
                    questions += 1
                elif RE_USAGE.search(line):
                    prompt_tokens += number_after("prompt", line) or 0
                    output_tokens += number_after("output", line) or 0
                else:
                    done = RE_DONE.search(line)
                    if done:
                        times.append(float(done.group(2)))
                        if done.group(1) != "ok":
                            errors += 1

    average = sum(times) / len(times) if times else 0
    print(f"Ngày: {day}")
    print(f"Số câu hỏi:          {questions}")
    print(f"Tổng prompt tokens:  {prompt_tokens}")
    print(f"Tổng output tokens:  {output_tokens}")
    print(f"Thời gian trả lời TB: {average:.2f} giây")
    print(f"Số câu bị lỗi:       {errors}")


if __name__ == "__main__":
    main()
