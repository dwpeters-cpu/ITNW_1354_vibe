"""
Import TXT, DOCX, and PDF course content into an empty Canvas course.

Safe mode:
    python import_course.py --source "C:\\Canvas Courses\\ITNW 1354"

Execute:
    python import_course.py --source "C:\\Canvas Courses\\ITNW 1354" --execute
"""

import argparse
import html
import os
import re
from pathlib import Path

from canvasapi import Canvas
from dotenv import load_dotenv

SUPPORTED = {".txt", ".docx", ".pdf"}


def arguments():
    parser = argparse.ArgumentParser(description="Import a folder-based course into Canvas.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def ordered_name(name):
    """Return (order, visible name) for '01 Name'."""
    match = re.match(r"^\s*(\d+)\s*(?:[-_.]\s*)?(.*)$", name)
    if match and match.group(2).strip():
        return int(match.group(1)), match.group(2).strip()
    return 9999, name.strip()


def sort_key(path):
    order, title = ordered_name(path.name)
    return order, title.casefold()


def find_file(folder, name):
    for path in folder.iterdir():
        if path.is_file() and path.name.casefold() == name.casefold():
            return path
    return None


def upload_name(path):
    return ordered_name(path.stem)[1] + path.suffix


def module_overview_title(module_title):
    match = re.match(r"^(Module\s+\d+)\b", module_title, re.IGNORECASE)
    return f"{match.group(1)} Overview" if match else f"{module_title} Overview"


def read_text(path):
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252")


def text_to_html(path):
    """Convert plain text to simple paragraphs without a Markdown dependency."""
    text = read_text(path).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "<p></p>"
    paragraphs = re.split(r"\n\s*\n", text)
    return "\n".join(
        f"<p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>"
        for paragraph in paragraphs
        if paragraph.strip()
    )


def txt_item(path):
    title = ordered_name(path.stem)[1]
    lower = title.casefold()

    if lower.startswith("assignment -"):
        return {"type": "assignment", "title": title.split("-", 1)[1].strip(), "path": path}
    if lower.startswith("discussion -"):
        return {"type": "discussion", "title": title.split("-", 1)[1].strip(), "path": path}
    return {"type": "page", "title": title, "path": path}


def file_item(path, canvas_folder):
    return {
        "type": "file",
        "title": ordered_name(path.stem)[1],
        "path": path,
        "upload_name": upload_name(path),
        "canvas_folder": canvas_folder,
    }


def scan_source(source):
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Source folder does not exist: {source}")

    plan = {
        "source": source,
        "description": None,
        "course_files": [],
        "modules": [],
        "warnings": [],
    }

    folders = [path for path in source.iterdir() if path.is_dir()]
    course_info = None

    for folder in folders:
        if ordered_name(folder.name)[1].casefold() == "course information":
            course_info = folder
            break

    if course_info:
        plan["description"] = find_file(course_info, "Course Description.txt")
        for path in sorted(course_info.iterdir(), key=sort_key):
            if path.is_file() and path.suffix.casefold() in {".docx", ".pdf"}:
                plan["course_files"].append(file_item(path, "Course Information"))
    else:
        plan["warnings"].append("00 Course Information folder was not found.")

    module_folders = []
    for folder in folders:
        if folder == course_info:
            continue
        order, _ = ordered_name(folder.name)
        if order == 9999:
            plan["warnings"].append(f"Ignored unnumbered folder: {folder.name}")
        else:
            module_folders.append(folder)

    for module_folder in sorted(module_folders, key=sort_key):
        module_order, module_title = ordered_name(module_folder.name)
        overview_path = find_file(module_folder, "Module Overview.txt")
        module = {
            "order": module_order,
            "title": module_title,
            "overview": None,
            "lessons": [],
        }

        if overview_path:
            module["overview"] = {
                "type": "page",
                "title": module_overview_title(module_title),
                "path": overview_path,
            }

        lesson_folders = [path for path in module_folder.iterdir() if path.is_dir()]
        for lesson_folder in sorted(lesson_folders, key=sort_key):
            lesson_order, lesson_title = ordered_name(lesson_folder.name)
            if lesson_order == 9999:
                plan["warnings"].append(f"Ignored unnumbered lesson: {lesson_folder}")
                continue

            overview_path = find_file(lesson_folder, "Lesson Overview.txt")
            lesson = {
                "order": lesson_order,
                "title": lesson_title,
                "overview": None,
                "items": [],
            }

            if overview_path:
                lesson["overview"] = {
                    "type": "page",
                    "title": lesson_title,
                    "path": overview_path,
                }

            for path in sorted(lesson_folder.iterdir(), key=sort_key):
                if not path.is_file() or path == overview_path:
                    continue
                suffix = path.suffix.casefold()
                if suffix not in SUPPORTED:
                    plan["warnings"].append(f"Ignored unsupported file: {path}")
                elif suffix == ".txt":
                    lesson["items"].append(txt_item(path))
                else:
                    canvas_folder = f"{module_title}/{lesson_title}"
                    lesson["items"].append(file_item(path, canvas_folder))

            module["lessons"].append(lesson)

        plan["modules"].append(module)

    if not plan["modules"]:
        raise ValueError("No numbered module folders were found.")

    return plan


def lesson_items(plan):
    return [
        item
        for module in plan["modules"]
        for lesson in module["lessons"]
        for item in lesson["items"]
    ]


def counts(plan):
    items = lesson_items(plan)
    all_files = plan["course_files"] + [item for item in items if item["type"] == "file"]
    return {
        "modules": len(plan["modules"]),
        "lessons": sum(len(module["lessons"]) for module in plan["modules"]),
        "module_pages": sum(bool(module["overview"]) for module in plan["modules"]),
        "lesson_pages": sum(
            bool(lesson["overview"])
            for module in plan["modules"]
            for lesson in module["lessons"]
        ),
        "other_pages": sum(item["type"] == "page" for item in items),
        "assignments": sum(item["type"] == "assignment" for item in items),
        "discussions": sum(item["type"] == "discussion" for item in items),
        "docx": sum(item["path"].suffix.casefold() == ".docx" for item in all_files),
        "pdf": sum(item["path"].suffix.casefold() == ".pdf" for item in all_files),
    }


def show_plan(plan):
    total = counts(plan)
    print("\n" + "=" * 68)
    print("IMPORT PLAN")
    print("=" * 68)
    print(f"Source folder:          {plan['source']}")
    print(f"Course description:     {'Yes' if plan['description'] else 'No'}")
    print(f"Modules:                {total['modules']}")
    print(f"Lessons:                {total['lessons']}")
    print(f"Module overview pages:  {total['module_pages']}")
    print(f"Lesson overview pages:  {total['lesson_pages']}")
    print(f"Other pages:            {total['other_pages']}")
    print(f"Assignments:            {total['assignments']}")
    print(f"Discussions:            {total['discussions']}")
    print(f"DOCX files:             {total['docx']}")
    print(f"PDF files:              {total['pdf']}")

    print("\nStructure:")
    for module in plan["modules"]:
        print(f"  {module['order']:02d}. {module['title']}")
        for lesson in module["lessons"]:
            marker = "" if lesson["overview"] else " (no overview)"
            print(f"      {lesson['order']:02d}. {lesson['title']}{marker}")
            for item in lesson["items"]:
                print(f"          [{item['type'].title()}] {item['title']}")

    if plan["warnings"]:
        print("\nWarnings:")
        for warning in plan["warnings"]:
            print(f"  - {warning}")


def assignment_group(course):
    for group in course.get_assignment_groups():
        if group.name.strip().casefold() == "assignments":
            return group
    return course.create_assignment_group(name="Assignments")


def upload_file(course, item):
    success, response = course.upload(
        str(item["path"]),
        name=item["upload_name"],
        parent_folder_path=item["canvas_folder"],
        on_duplicate="rename",
    )
    if not success or not isinstance(response, dict) or "id" not in response:
        raise RuntimeError(f"File upload failed: {item['path']}\nResponse: {response}")
    item["canvas_id"] = int(response["id"])


def create_page(course, item):
    page = course.create_page(
        {
            "title": item["title"],
            "body": text_to_html(item["path"]),
            "published": False,
        }
    )
    item["page_url"] = page.url


def create_assignment(course, item, group_id):
    assignment = course.create_assignment(
        {
            "name": item["title"],
            "description": text_to_html(item["path"]),
            "points_possible": 100,
            "submission_types": ["online_upload"],
            "allowed_extensions": ["docx", "pdf"],
            "assignment_group_id": group_id,
            "published": False,
        }
    )
    item["canvas_id"] = int(assignment.id)


def create_discussion(course, item):
    discussion = course.create_discussion_topic(
        title=item["title"],
        message=text_to_html(item["path"]),
        discussion_type="threaded",
        published=False,
    )
    item["canvas_id"] = int(discussion.id)


def add_module_item(module, item):
    item_type = item["type"]
    if item_type == "page":
        data = {"type": "Page", "page_url": item["page_url"], "title": item["title"]}
    elif item_type == "assignment":
        data = {"type": "Assignment", "content_id": item["canvas_id"], "title": item["title"]}
    elif item_type == "discussion":
        data = {"type": "Discussion", "content_id": item["canvas_id"], "title": item["title"]}
    elif item_type == "file":
        data = {"type": "File", "content_id": item["canvas_id"], "title": item["title"]}
    else:
        raise ValueError(f"Unsupported module item type: {item_type}")
    module.create_module_item(data)


def execute(course, plan):
    group = assignment_group(course)
    items = lesson_items(plan)

    if plan["description"]:
        course.update(course={"syllabus_body": text_to_html(plan["description"])})
        print("✅ Added Course Description.txt to the Canvas syllabus")

    files = plan["course_files"] + [item for item in items if item["type"] == "file"]
    for item in files:
        print(f"⬆️  Uploading {item['upload_name']}")
        upload_file(course, item)

    pages = []
    for module in plan["modules"]:
        if module["overview"]:
            pages.append(module["overview"])
        for lesson in module["lessons"]:
            if lesson["overview"]:
                pages.append(lesson["overview"])
            pages.extend(item for item in lesson["items"] if item["type"] == "page")

    for item in pages:
        print(f"📄 Creating page: {item['title']}")
        create_page(course, item)

    for item in items:
        if item["type"] == "assignment":
            print(f"📝 Creating assignment: {item['title']}")
            create_assignment(course, item, int(group.id))
        elif item["type"] == "discussion":
            print(f"💬 Creating discussion: {item['title']}")
            create_discussion(course, item)

    for module_data in plan["modules"]:
        print(f"📚 Creating module: {module_data['title']}")
        module = course.create_module(
            {
                "name": module_data["title"],
                "position": module_data["order"],
                "published": False,
            }
        )

        if module_data["overview"]:
            add_module_item(module, module_data["overview"])

        for lesson in module_data["lessons"]:
            module.create_module_item(
                {"type": "SubHeader", "title": lesson["title"], "indent": 0}
            )
            if lesson["overview"]:
                add_module_item(module, lesson["overview"])
            for item in lesson["items"]:
                add_module_item(module, item)

    total = counts(plan)
    print("\n" + "=" * 68)
    print("IMPORT COMPLETE")
    print("=" * 68)
    print(f"Modules created:       {total['modules']}")
    print(f"Lessons created:       {total['lessons']}")
    print(f"Assignments created:   {total['assignments']}")
    print(f"Discussions created:   {total['discussions']}")
    print(f"Files uploaded:        {total['docx'] + total['pdf']}")
    print("All imported content was created unpublished.")


def main():
    args = arguments()
    plan = scan_source(args.source)

    load_dotenv()
    canvas_url = os.getenv("CANVAS_URL")
    canvas_token = os.getenv("CANVAS_TOKEN")
    course_id_text = os.getenv("COURSE_ID")

    if not all([canvas_url, canvas_token, course_id_text]):
        raise ValueError(".env must contain CANVAS_URL, CANVAS_TOKEN, and COURSE_ID")

    try:
        course_id = int(course_id_text)
    except ValueError as error:
        raise ValueError(f"COURSE_ID must be numeric: {course_id_text}") from error

    print(f"\n🔗 Connecting to Canvas: {canvas_url}")
    canvas = Canvas(canvas_url, canvas_token)
    course = canvas.get_course(course_id)

    print("\n✅ Successfully connected!")
    print(f"   Destination Course: {course.name}")
    print(f"   Course ID: {course.id}")

    existing_modules = list(course.get_modules())
    print(f"   Existing modules: {len(existing_modules)}")
    show_plan(plan)

    if not args.execute:
        print("\nSAFE MODE")
        print("No changes will be made.")
        return

    if existing_modules:
        print("\n❌ Import stopped: the destination course already contains modules.")
        print("Version one is intended for an empty Canvas course.")
        return

    try:
        execute(course, plan)
    except Exception as error:
        print(f"\n❌ Import stopped: {error}")
        print("Some content may already have been created. Review the sandbox before rerunning.")
        raise


if __name__ == "__main__":
    main()
