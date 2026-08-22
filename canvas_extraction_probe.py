import os
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

COURSE_ID = 11517

CANVAS_TOKEN = (
    os.getenv("CANVAS_API_KEY")
    or os.getenv("CANVAS_API_TOKEN")
    or os.getenv("CANVAS_TOKEN")
)

CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL")

if not CANVAS_TOKEN:
    raise RuntimeError("Canvas API token not found in .env")

if not CANVAS_BASE_URL:
    raise RuntimeError("CANVAS_BASE_URL not found in .env")

CANVAS_BASE_URL = CANVAS_BASE_URL.rstrip("/")

headers = {
    "Authorization": f"Bearer {CANVAS_TOKEN}"
}


def get_front_page():
    url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/front_page"

    response = requests.get(
        url,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    return response.json()["body"]


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_agenda(html):
    soup = BeautifulSoup(html, "html.parser")

    # ---------------------------------------------------------
    # Find "Week of August 17 to August 20"
    # ---------------------------------------------------------

    week_node = soup.find(
        string=lambda text:
            text
            and re.search(
                r"Week of\s+[A-Za-z]+\s+\d+\s+to\s+[A-Za-z]*\s*\d+",
                text,
                re.IGNORECASE
            )
    )

    if not week_node:
        raise RuntimeError("Could not find the weekly agenda heading.")

    week_match = re.search(
        r"Week of\s+[A-Za-z]+\s+\d+\s+to\s+(?:[A-Za-z]+\s+)?\d+",
        clean(week_node),
        re.IGNORECASE
    )

    week = week_match.group(0)

    # ---------------------------------------------------------
    # Find the agenda's outer container
    #
    # Move upward until we find a parent containing:
    # - Learning Targets
    # - Learning Activities
    # - Monday-Friday
    # ---------------------------------------------------------

    agenda = week_node.parent

    while agenda:
        text = clean(agenda.get_text(" ", strip=True))

        has_targets = "Learning Targets for the Week" in text
        has_activities = "Learning Activities" in text
        has_assignments = "Assignments" in text

        if has_targets and has_activities and has_assignments:
            break

        agenda = agenda.parent

    if agenda is None:
        raise RuntimeError("Could not locate the full agenda container.")

    # ---------------------------------------------------------
    # Extract learning target
    # ---------------------------------------------------------

    target_node = agenda.find(
        string=lambda text:
            text
            and "Learning Targets for the Week" in text
    )

    target = ""

    if target_node:
        parent_text = clean(target_node.parent.get_text(" ", strip=True))

        target = re.sub(
            r"^Learning Targets for the Week:\s*",
            "",
            parent_text,
            flags=re.IGNORECASE
        ).strip()

    # Sometimes the label and target are in separate HTML elements
    if not target:
        agenda_text = clean(agenda.get_text(" ", strip=True))

        match = re.search(
            r"Learning Targets for the Week:\s*(.*?)"
            r"(?=Learning Activities)",
            agenda_text,
            re.IGNORECASE
        )

        if match:
            target = clean(match.group(1))

    # ---------------------------------------------------------
    # Extract the weekday table
    # ---------------------------------------------------------

    weekdays = ["M", "T", "W", "Th", "F"]

    rows = {
        day: ["", ""]
        for day in weekdays
    }

    tables = agenda.find_all("table")

    agenda_table = None

    for table in tables:
        table_text = clean(table.get_text(" ", strip=True))

        if (
            "Learning Activities" in table_text
            and "Assignments" in table_text
        ):
            agenda_table = table
            break

    if agenda_table:
        for tr in agenda_table.find_all("tr"):
            cells = tr.find_all(["td", "th"])

            values = [
                clean(cell.get_text(" ", strip=True))
                for cell in cells
            ]

            values = [v for v in values if v]

            if not values:
                continue

            # Find which cell contains the weekday
            day = None
            day_index = None

            for i, value in enumerate(values):
                normalized = value.rstrip(".")

                if normalized in weekdays:
                    day = normalized
                    day_index = i
                    break

            if day is None:
                continue

            remaining = values[day_index + 1:]

            activity = remaining[0] if len(remaining) >= 1 else ""
            assignment = remaining[1] if len(remaining) >= 2 else ""

            rows[day] = [activity, assignment]

    # ---------------------------------------------------------
    # Fallback for layouts that aren't actual HTML tables
    # ---------------------------------------------------------

    if not any(rows[day][0] or rows[day][1] for day in weekdays):
        agenda_text = agenda.get_text("\n", strip=True)

        lines = [
            clean(line)
            for line in agenda_text.splitlines()
            if clean(line)
        ]

        for i, line in enumerate(lines):
            if line in weekdays:
                content = ""

                if i + 1 < len(lines):
                    next_line = lines[i + 1]

                    if next_line not in weekdays:
                        content = next_line

                rows[line][0] = content

    # ---------------------------------------------------------
    # Output ONLY the requested agenda
    # ---------------------------------------------------------

    print(f"🗓️ {week}")
    print("Learning Targets for the Week:")
    print(target)
    print("| Learning Activities | Assignments")

    for day in weekdays:
        activity, assignment = rows[day]
        print(f"{day} | {activity} | {assignment}")


if __name__ == "__main__":
    html = get_front_page()
    extract_agenda(html)