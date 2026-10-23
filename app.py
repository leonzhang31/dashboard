import calendar as calendar_module
import hashlib
import sqlite3
import ssl
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import certifi
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from f1 import get_f1_info

app = Flask(__name__)
DATABASE = Path(__file__).parent / "homework.db"
COURSES = [
    "English",
    "Geography",
    "Math",
    "Music",
    "Design",
    "Science",
    "Art",
    "French",
    "Other",
]
calendar_sync_lock = threading.Lock()


def get_db_connection():
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_db_connection()
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            due_date TEXT NOT NULL DEFAULT '',
            course TEXT NOT NULL DEFAULT '',
            completed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(homework)").fetchall()
    }
    if "course" not in columns:
        connection.execute(
            "ALTER TABLE homework ADD COLUMN course TEXT NOT NULL DEFAULT ''"
        )
    if "due_time" not in columns:
        connection.execute("ALTER TABLE homework ADD COLUMN due_time TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            calendar_url TEXT NOT NULL DEFAULT '',
            calendar_source TEXT NOT NULL DEFAULT 'none',
            ics_content TEXT NOT NULL DEFAULT '',
            last_synced TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO calendar_settings (id)
        VALUES (1)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            uid TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.commit()
    connection.close()


def parse_ics_date(value, timezone_name=""):
    value = value.split(":", 1)[-1].strip()
    is_utc = value.endswith("Z")
    if is_utc:
        value = value[:-1]
    for date_format in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, date_format)
            if date_format == "%Y%m%d":
                return parsed.strftime("%Y-%m-%d")
            if is_utc:
                parsed = parsed.replace(tzinfo=timezone.utc).astimezone()
            elif timezone_name:
                try:
                    parsed = parsed.replace(
                        tzinfo=ZoneInfo(timezone_name)
                    ).astimezone()
                except ZoneInfoNotFoundError:
                    pass
            parsed = parsed.replace(tzinfo=None)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return value


def format_time_label(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        return value
    label = parsed.strftime("%I:%M %p").lstrip("0")
    if parsed.hour == 12 and parsed.minute == 0:
        label += " (noon)"
    return label


def parse_ics(ics_content):
    lines = []
    for line in ics_content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    events = []
    uid_counts = {}
    current = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            if current.get("UID") and current.get("DTSTART") and current.get("SUMMARY"):
                original_uid = current["UID"]
                occurrence_number = uid_counts.get(original_uid, 0) + 1
                uid_counts[original_uid] = occurrence_number
                event_uid = original_uid
                if occurrence_number > 1:
                    event_uid = f"{original_uid}#{occurrence_number}"
                events.append(
                    {
                        "uid": event_uid,
                        "title": current["SUMMARY"],
                        "start_time": parse_ics_date(
                            current["DTSTART"], current.get("DTSTART_TIMEZONE", "")
                        ),
                        "end_time": parse_ics_date(
                            current.get("DTEND", ""),
                            current.get("DTEND_TIMEZONE", ""),
                        ),
                        "description": current.get("DESCRIPTION", ""),
                        "location": current.get("LOCATION", ""),
                    }
                )
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            key_parts = key.split(";")
            field_name = key_parts[0]
            timezone_name = ""
            for parameter in key_parts[1:]:
                if parameter.startswith("TZID="):
                    timezone_name = parameter.removeprefix("TZID=")
            if field_name in {"DTSTART", "DTEND"}:
                current[f"{field_name}_TIMEZONE"] = timezone_name
            current[field_name] = value.replace("\\n", "\n").replace("\\,", ",")
    return events


ROTATION_START_DATE = date(2026, 9, 11)
ROTATION_BLOCK_LENGTH = 18
CALENDAR_START_MINUTE = 6 * 60
CALENDAR_END_MINUTE = 22 * 60
CALENDAR_TOTAL_MINUTES = CALENDAR_END_MINUTE - CALENDAR_START_MINUTE


def calendar_time_scope():
    return CALENDAR_START_MINUTE, CALENDAR_END_MINUTE


def format_minute_label(minute):
    minute %= 24 * 60
    return datetime(2000, 1, 1, minute // 60, minute % 60).strftime(
        "%I:%M %p"
    ).lstrip("0")


def calendar_event_identity(event):
    event_data = dict(event)
    title = " ".join(event_data["title"].casefold().split())
    location = " ".join(event_data.get("location", "").casefold().split())
    return f"{title}|{location}"


CALENDAR_PALETTE = (
    "#7cb4cf", "#c29bce", "#e0b575", "#8dbb9e",
    "#d796a3", "#939fd3", "#cdab8b", "#7dbdb5",
)
CALENDAR_SUBJECT_COLORS = {
    "english": "#c29bce", "geography": "#8dbb9e", "math": "#7cb4cf",
    "music": "#d796a3", "design": "#e0b575", "science": "#7dbdb5",
    "art": "#cdab8b", "french": "#939fd3",
}


def calendar_event_color(identity):
    title = identity.partition("|")[0].casefold()
    if title.startswith("homework due:"):
        return "#ff8c69"
    words = set(title.replace("-", " ").split())
    for subject, color in CALENDAR_SUBJECT_COLORS.items():
        if subject in words:
            return color
    color_hash = int(hashlib.sha256(identity.encode("utf-8")).hexdigest(), 16)
    return CALENDAR_PALETTE[color_hash % len(CALENDAR_PALETTE)]


def recurring_event_colors(identities):
    return {identity: calendar_event_color(identity) for identity in identities}


def school_day_dates(events):
    events_by_date = {}
    for event in events:
        try:
            event_date = datetime.strptime(
                event["start_time"], "%Y-%m-%d %H:%M"
            ).date()
        except (KeyError, ValueError):
            continue
        if event_date.weekday() < 5:
            events_by_date.setdefault(event_date, 0)
            events_by_date[event_date] += 1
    return sorted(
        event_date
        for event_date, event_count in events_by_date.items()
        if event_count >= 2 and event_date >= ROTATION_START_DATE
    )


def school_days_from_rotation_start(day, events):
    qualifying_dates = school_day_dates(events)
    return sum(event_date <= day for event_date in qualifying_dates) - 1


def active_rotating_subject(day, events):
    school_day_index = school_days_from_rotation_start(day, events)
    block = (school_day_index // ROTATION_BLOCK_LENGTH) % 2
    return "Music" if block == 0 else "Art"


def is_event_active_for_rotation(title, day, events):
    title_lower = title.lower()
    if "music" not in title_lower and "art" not in title_lower:
        return True
    return active_rotating_subject(day, events).lower() in title_lower


def get_rotation_info(day, events):
    qualifying_dates = school_day_dates(events)
    completed_days = sum(event_date <= day for event_date in qualifying_dates)
    if completed_days:
        cycle_day = ((completed_days - 1) % 9) + 1
        block_number = ((completed_days - 1) // ROTATION_BLOCK_LENGTH) + 1
        subject = "Music" if ((completed_days - 1) // ROTATION_BLOCK_LENGTH) % 2 == 0 else "Art"
    else:
        cycle_day = None
        block_number = None
        subject = "Music"
    return {
        "cycle_day": cycle_day,
        "block_number": block_number,
        "subject": subject,
        "qualifying_dates": qualifying_dates,
    }


def prepare_calendar_events(
    events, rotation_events=None, scope_start_minute=None, scope_end_minute=None
):
    rotation_events = events if rotation_events is None else rotation_events
    if scope_start_minute is None or scope_end_minute is None:
        scope_start_minute, scope_end_minute = calendar_time_scope()
    scope_minutes = scope_end_minute - scope_start_minute
    occurrence_dates = {}
    for event in events:
        try:
            event_date = datetime.strptime(
                event["start_time"], "%Y-%m-%d %H:%M"
            ).date()
        except (KeyError, ValueError):
            continue
        identity = calendar_event_identity(event)
        occurrence_dates.setdefault(identity, set()).add(event_date)
    recurring_colors = recurring_event_colors(
        identity
        for identity, dates in occurrence_dates.items()
        if len(dates) > 1
    )

    events_by_day = {}
    seen_events = set()
    for event in events:
        try:
            start = datetime.strptime(event["start_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if not is_event_active_for_rotation(
            event["title"], start.date(), rotation_events
        ):
            continue
        try:
            end = datetime.strptime(event["end_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            end = start
        if end < start:
            end = start
        event_data = dict(event)
        event_key = (
            event_data["title"],
            start,
            end,
            event_data.get("location", ""),
            event_data.get("description", ""),
        )
        if event_key in seen_events:
            continue
        seen_events.add(event_key)

        identity = calendar_event_identity(event_data)
        event_data["color"] = recurring_colors.get(
            identity, calendar_event_color(identity)
        )
        event_data["start_label"] = format_time_label(event["start_time"])
        event_data["end_label"] = (
            format_time_label(event["end_time"]) if end > start else ""
        )
        duration_minutes = max(30, int((end - start).total_seconds() / 60))
        start_minute = start.hour * 60 + start.minute
        end_minute = start_minute + duration_minutes
        if end_minute <= scope_start_minute or start_minute >= scope_end_minute:
            continue
        visible_start = max(start_minute, scope_start_minute)
        visible_end = min(end_minute, scope_end_minute)
        event_data["top_percent"] = max(
            0, (visible_start - scope_start_minute) / scope_minutes * 100
        )
        event_data["height_percent"] = max(
            3, (visible_end - visible_start) / scope_minutes * 100
        )
        event_data["duration_label"] = f"{duration_minutes // 60}h {duration_minutes % 60:02d}m"
        events_by_day.setdefault(start.date().isoformat(), []).append(
            (start, end, event_data)
        )

    prepared_events = {}
    for day, day_events in events_by_day.items():
        day_events.sort(key=lambda item: item[0])
        prepared_events[day] = []
        groups = []
        current_group = []
        group_end = None
        for start, end, event_data in day_events:
            visual_end = max(end, start + timedelta(minutes=30))
            if group_end is None or start < group_end:
                current_group.append((start, visual_end, event_data))
            else:
                groups.append(current_group)
                current_group = [(start, visual_end, event_data)]
            group_end = max(group_end or visual_end, visual_end)
        if current_group:
            groups.append(current_group)

        for group in groups:
            lane_ends = []
            for start, visual_end, event_data in group:
                lane = next(
                    (
                        index
                        for index, lane_end in enumerate(lane_ends)
                        if lane_end <= start
                    ),
                    len(lane_ends),
                )
                if lane == len(lane_ends):
                    lane_ends.append(visual_end)
                else:
                    lane_ends[lane] = visual_end
                event_data["lane"] = lane
                event_data["lane_count"] = len(lane_ends)
                prepared_events[day].append(event_data)

            lane_count = len(lane_ends)
            for event_data in prepared_events[day][-len(group):]:
                event_data["lane_count"] = lane_count
    return prepared_events


def sync_calendar(source, calendar_url="", ics_content=""):
    if source == "url":
        parsed_url = urlparse(calendar_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Please enter a valid http:// or https:// calendar URL.")
        request_for_url = Request(calendar_url, headers={"User-Agent": "school-dashboard"})
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            with urlopen(request_for_url, timeout=10, context=ssl_context) as response:
                ics_content = response.read().decode("utf-8")
        except HTTPError as error:
            if error.code == 404:
                raise ValueError(
                    "Calendar refresh failed: the calendar server returned "
                    "HTTP 404. Previously synced events are still displayed; "
                    "check the Last synced time and verify the feed is still "
                    "available."
                ) from error
            if error.code == 401:
                raise ValueError(
                    "Calendar URL returned 401 Unauthorized. Copy a current "
                    "Secret address in iCal format from Google Calendar and "
                    "paste the complete URL without quotes or extra spaces."
                ) from error
            raise ValueError(
                f"Calendar server returned HTTP error {error.code}."
            ) from error
        except (OSError, URLError, UnicodeDecodeError) as error:
            raise ValueError(f"Could not download the calendar: {error}") from error

    events = parse_ics(ics_content)
    with calendar_sync_lock:
        connection = get_db_connection()
        try:
            connection.execute("DELETE FROM calendar_events")
            connection.executemany(
                """
                INSERT INTO calendar_events
                    (uid, title, start_time, end_time, description, location)
                VALUES (:uid, :title, :start_time, :end_time, :description, :location)
                """,
                events,
            )
            connection.execute(
                """
                UPDATE calendar_settings
                SET calendar_url = ?, calendar_source = ?, ics_content = ?, last_synced = ?
                WHERE id = 1
                """,
                (
                    calendar_url,
                    source,
                    ics_content if source == "upload" else "",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            connection.commit()
        finally:
            connection.close()


def get_homework(homework_id):
    connection = get_db_connection()
    homework = connection.execute(
        "SELECT * FROM homework WHERE id = ?", (homework_id,)
    ).fetchone()
    connection.close()

    if homework is None:
        abort(404)
    return homework


def get_dashboard_context():
    now = datetime.now()
    connection = get_db_connection()
    all_events = connection.execute(
        """
        SELECT title, start_time, end_time, location
        FROM calendar_events
        ORDER BY start_time
        """
    ).fetchall()
    connection.close()
    events = [
        event
        for event in all_events
        if event["start_time"].startswith(now.strftime("%Y-%m-%d"))
    ]

    current_class = None
    next_class = None
    for event in events:
        try:
            start = datetime.strptime(event["start_time"], "%Y-%m-%d %H:%M")
            end = datetime.strptime(event["end_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if not is_event_active_for_rotation(
            event["title"], now.date(), all_events
        ):
            continue
        title_lower = event["title"].lower()
        simplified_name = next(
            (
                course
                for course in COURSES
                if course.lower() in title_lower
            ),
            event["title"],
        )
        if start <= now < end:
            current_class = {
                "name": simplified_name,
                "location": event["location"],
            }
            break
        if start > now and next_class is None:
            next_class = {
                "name": simplified_name,
                "location": event["location"],
                "start_time": format_time_label(event["start_time"]),
            }

    hour = now.hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return {
        "greeting": greeting,
        "current_class": current_class,
        "next_class": next_class,
        "rotation_info": get_rotation_info(now.date(), all_events),
    }


@app.context_processor
def inject_dashboard_context():
    return get_dashboard_context()


@app.get("/api/current-class")
def current_class_api():
    dashboard_context = get_dashboard_context()
    return jsonify(
        {
            "current_class": dashboard_context["current_class"],
            "next_class": dashboard_context["next_class"],
        }
    )


@app.get("/api/f1")
def f1_api():
    response = jsonify(get_f1_info())
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get("/api/next-class")
def next_class_api():
    course = request.args.get("course", "").strip()
    if not course:
        return jsonify({"next_class": None})

    now = datetime.now()
    connection = get_db_connection()
    all_events = connection.execute(
        """
        SELECT title, start_time, end_time, location
        FROM calendar_events
        ORDER BY start_time
        """
    ).fetchall()
    connection.close()

    matching_events = []
    course_lower = course.lower()
    for event in all_events:
        title_lower = event["title"].lower()
        if course_lower == "other":
            continue
        if course_lower not in title_lower:
            continue
        try:
            start = datetime.strptime(event["start_time"], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if start < now or not is_event_active_for_rotation(
            event["title"], start.date(), all_events
        ):
            continue
        matching_events.append((start, event))

    if not matching_events:
        return jsonify({"next_class": None})

    start, event = matching_events[0]
    return jsonify(
        {
            "next_class": {
                "date": start.date().isoformat(),
                "start_time": format_time_label(event["start_time"]),
                "time": start.strftime("%H:%M"),
                "title": event["title"],
            }
        }
    )


initialize_database()


@app.route("/")
@app.route("/homework", endpoint="homework_list")
def home():
    connection = get_db_connection()
    homework = connection.execute(
        "SELECT * FROM homework ORDER BY completed, due_date, id"
    ).fetchall()
    due_tomorrow = connection.execute(
        """
        SELECT * FROM homework
        WHERE completed = 0 AND due_date = ?
        ORDER BY course, id
        """,
        ((date.today() + timedelta(days=1)).isoformat(),),
    ).fetchall()
    connection.close()
    return render_template(
        "home.html",
        homework=homework,
        due_tomorrow=due_tomorrow,
    )


@app.route("/calendar", methods=["GET", "POST"])
def calendar():
    requested_week = request.args.get("week", "")
    is_dashboard_tab_request = request.headers.get("X-Dashboard-Tab") == "true"
    connection = get_db_connection()
    settings = connection.execute(
        "SELECT * FROM calendar_settings WHERE id = 1"
    ).fetchone()
    connection.close()
    error = None

    if request.method == "POST":
        source = request.form.get("source", "url")
        calendar_url = request.form.get("calendar_url", "").strip()
        uploaded_file = request.files.get("calendar_file")
        try:
            if source == "upload":
                if uploaded_file is None or not uploaded_file.filename.lower().endswith(".ics"):
                    raise ValueError("Please choose an .ics calendar file.")
                sync_calendar(
                    "upload",
                    ics_content=uploaded_file.read().decode("utf-8"),
                )
            else:
                sync_calendar("url", calendar_url=calendar_url)
        except (ValueError, UnicodeDecodeError) as sync_error:
            error = str(sync_error)
        else:
            return redirect(url_for("calendar"))

        connection = get_db_connection()
        settings = connection.execute(
            "SELECT * FROM calendar_settings WHERE id = 1"
        ).fetchone()
        connection.close()

    should_sync_calendar = False
    if not requested_week and settings["calendar_source"] == "url" and settings["calendar_url"]:
        try:
            last_synced = datetime.strptime(
                settings["last_synced"], "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            should_sync_calendar = True
        else:
            should_sync_calendar = datetime.now() - last_synced >= timedelta(minutes=15)

    if not error and should_sync_calendar and not is_dashboard_tab_request:
        try:
            sync_calendar("url", calendar_url=settings["calendar_url"])
        except ValueError as sync_error:
            error = str(sync_error)

    connection = get_db_connection()
    all_events = connection.execute(
        "SELECT * FROM calendar_events ORDER BY start_time"
    ).fetchall()
    homework_reminders = connection.execute(
        """
        SELECT id, title, course, due_date, due_time
        FROM homework
        WHERE completed = 0 AND due_date != ''
        ORDER BY due_date, id
        """
    ).fetchall()
    connection.close()
    reminder_events = []
    for homework in homework_reminders:
        try:
            due_day = datetime.strptime(homework["due_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        due_time = homework["due_time"]
        try:
            due_start = datetime.strptime(f"{due_day.isoformat()} {due_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            due_start = None
        reminder_events.append(
            {
                "uid": f"homework-{homework['id']}",
                "title": f"Homework due: {homework['title']}",
                "start_time": due_start.strftime("%Y-%m-%d %H:%M") if due_start else f"{due_day.isoformat()} 06:00",
                "end_time": due_start.strftime("%Y-%m-%d %H:%M") if due_start else f"{due_day.isoformat()} 08:00",
                "is_homework": True,
                "description": "Due at class start." if due_start else "Homework reminder — no due time set.",
                "location": homework["course"],
            }
        )
    calendar_events = list(all_events) + reminder_events
    today = date.today()
    try:
        selected_date = datetime.strptime(requested_week, "%Y-%m-%d").date()
    except ValueError:
        selected_date = today
    week_start = selected_date - timedelta(days=(selected_date.weekday() + 1) % 7)
    week_days = [week_start + timedelta(days=index) for index in range(7)]
    week_end = week_days[-1]
    current_week = (
        f"{week_start.strftime('%B')} {week_start.day} - "
        f"{week_end.strftime('%B')} {week_end.day}, {week_end.year}"
    )
    scope_start_minute, scope_end_minute = calendar_time_scope()
    all_events_by_day = prepare_calendar_events(
        calendar_events,
        all_events,
        scope_start_minute,
        scope_end_minute,
    )
    events_by_day = {
        day: events
        for day, events in all_events_by_day.items()
        if week_start.isoformat() <= day <= week_end.isoformat()
    }
    previous_week = week_start - timedelta(days=7)
    next_week = week_start + timedelta(days=7)

    return render_template(
        "calendar.html",
        settings=settings,
        events_by_day=events_by_day,
        week_days=week_days,
        current_week=current_week,
        week_start=week_start,
        previous_week=previous_week,
        next_week=next_week,
        rotation_info=get_rotation_info(today, all_events),
        scope_start_minute=scope_start_minute,
        scope_end_minute=scope_end_minute,
        scope_start_label=format_minute_label(scope_start_minute),
        scope_end_label=format_minute_label(scope_end_minute),
        today=today,
        error=error,
        homework_reminders=homework_reminders,
    )


@app.route("/homework/add", methods=["GET", "POST"])
def add_homework():
    if request.method == "POST":
        title = request.form["title"].strip()
        if not title:
            return render_template(
                "homework_form.html",
                page_title="Add homework",
                button_text="Add homework",
                homework=request.form,
                courses=COURSES,
                error="A title is required.",
            ), 400

        connection = get_db_connection()
        connection.execute(
            """
            INSERT INTO homework (title, description, due_date, course, due_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                title,
                request.form.get("description", "").strip(),
                request.form.get("due_date", ""),
                request.form.get("course", "").strip(),
                request.form.get("due_time", ""),
            ),
        )
        connection.commit()
        connection.close()
        return redirect(url_for("home"))

    return render_template(
        "homework_form.html",
        page_title="Add homework",
        button_text="Add homework",
        homework=None,
        courses=COURSES,
    )


@app.route("/homework/<int:homework_id>/edit", methods=["GET", "POST"])
def edit_homework(homework_id):
    homework = get_homework(homework_id)

    if request.method == "POST":
        title = request.form["title"].strip()
        if not title:
            return render_template(
                "homework_form.html",
                page_title="Edit homework",
                button_text="Save changes",
                homework=request.form,
                courses=COURSES,
                error="A title is required.",
            ), 400

        connection = get_db_connection()
        connection.execute(
            """
            UPDATE homework
            SET title = ?, description = ?, due_date = ?, course = ?, due_time = ?
            WHERE id = ?
            """,
            (
                title,
                request.form.get("description", "").strip(),
                request.form.get("due_date", ""),
                request.form.get("course", "").strip(),
                request.form.get("due_time", ""),
                homework_id,
            ),
        )
        connection.commit()
        connection.close()
        return redirect(url_for("home"))

    return render_template(
        "homework_form.html",
        page_title="Edit homework",
        button_text="Save changes",
        homework=homework,
        courses=COURSES,
    )


@app.post("/homework/<int:homework_id>/complete")
def complete_homework(homework_id):
    get_homework(homework_id)
    connection = get_db_connection()
    connection.execute(
        "UPDATE homework SET completed = 1 WHERE id = ?", (homework_id,)
    )
    connection.commit()
    connection.close()
    return redirect(url_for("home"))


@app.post("/homework/<int:homework_id>/delete")
def delete_homework(homework_id):
    get_homework(homework_id)
    connection = get_db_connection()
    connection.execute("DELETE FROM homework WHERE id = ?", (homework_id,))
    connection.commit()
    connection.close()
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True)
