"""Extract Kuwait University student schedules from PDF files with OpenAI."""

from __future__ import annotations

import base64

from ..config import get_config
from ..openai_client import get_openai_client
from .schedule_models import ScheduleExtraction


SCHEDULE_EXTRACTION_PROMPT = """You are a document extraction system for Kuwait University student schedules.

Analyze the uploaded PDF and determine whether it is a valid Kuwait University student schedule.

Rules:
1. Extract only information explicitly present in the document. Never guess.
2. Return null for any scalar value that cannot be reliably extracted.
3. Normalize Arabic and English numerals to standard English digits.
4. Preserve Arabic names exactly as they appear in the document.
5. The PDF table layout may cause extracted text to appear out of order. Use the visual document structure and table relationships carefully.
6. Do not treat footer instructions, printing dates, or general university notes as student data.
7. Extract courses with statuses such as "منسحب" and preserve their status.
8. All non-null scalar values in the output must be strings.

A valid document should normally contain several of: جامعة الكويت, جدول الطالب,
رقم الطالب, اسم الطالب, الفصل, الكلية, التخصص, and a course schedule table.
If it is not a valid Kuwait University student schedule, set isValid=false,
explain why in invalidReason, use null/empty values elsewhere, and do not invent data.

fingerprintData is data for the backend to canonicalize and hash. The model must
never calculate a hash. Populate it as follows:
- studentId: English digits only.
- academicYear: YYYY/YYYY.
- semester: exactly the schedule identity "الأول", "الثاني", or "الصيفي" when evident.
- courses: every distinct course, containing only courseCode, section, and status.
- normalize digits, remove unnecessary whitespace, and sort by courseCode then section.
- exclude printing date, filename, instructor, room, exam data, GPA, and all other fields.

rawText should contain meaningful extracted content, excluding repetitive footer
instructions when possible.
"""


async def extract_schedule(pdf_bytes: bytes, filename: str) -> ScheduleExtraction:
    client = get_openai_client()
    config = get_config()
    encoded_pdf = base64.b64encode(pdf_bytes).decode("ascii")

    response = await client.responses.parse(
        model=config.OPENAI_SCHEDULE_MODEL,
        instructions=SCHEDULE_EXTRACTION_PROMPT,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": filename,
                        "file_data": f"data:application/pdf;base64,{encoded_pdf}",
                        "detail": "high",
                    },
                    {
                        "type": "input_text",
                        "text": "Extract and validate the attached student schedule.",
                    },
                ],
            }
        ],
        text_format=ScheduleExtraction,
        store=False,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("The model did not return a parsed schedule extraction.")
    return parsed
