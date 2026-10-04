"""Admin and developer MCP tools for Canvas API."""

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.credentials import is_http_request_active
from ..core.csv_safety import csv_safe_cell
from ..core.path import canvas_path
from ..core.untrusted_content import fence_untrusted_inline
from ..core.validation import validate_params


def register_admin_tools(mcp: FastMCP) -> None:
    """Register admin/developer MCP tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_anonymization_status() -> str:
        """Get current data anonymization status and statistics."""
        from ..core.anonymization import get_anonymization_stats
        from ..core.config import _canvas_env_file_path, get_config

        config = get_config()
        stats = get_anonymization_stats()

        result = "🔒 Data Anonymization Status:\n\n"

        if config.enable_data_anonymization:
            result += "✅ **ANONYMIZATION ENABLED** - Supported identity fields are masked\n\n"
            result += "📊 Session Statistics:\n"
            result += f"  • Total unique students anonymized: {stats['total_anonymized_ids']}\n"
            result += f"  • Privacy protection: {stats['privacy_status']}\n"
            result += f"  • Debug logging: {'ON' if config.anonymization_debug else 'OFF'}\n\n"

            if stats['total_anonymized_ids'] > 0:
                result += "🎭 Anonymous ID Examples:\n"
                for i, (real_hint, anon_id) in enumerate(stats['sample_mappings'].items()):
                    result += f"  • {real_hint} → {anon_id}\n"
                    if i >= 2:  # Limit to 3 examples
                        break
                result += "\n"

            result += "🛡️ **Privacy Control**: Supported identity fields are anonymized before tool output\n"
            result += "📍 **Data Path**: Tool results still pass to your configured AI client\n"

        else:
            result += "⚠️ **ANONYMIZATION DISABLED** - Tool output may include student identifiers\n\n"
            result += "🚨 **PRIVACY RISK**: Real student names and data may be sent to the AI client\n"
            result += "⚖️ **COMPLIANCE**: Review your institution's FERPA and data-handling requirements\n\n"
            result += f"💡 **Recommendation**: Enable anonymization in {_canvas_env_file_path()}:\n"
            result += "   ENABLE_DATA_ANONYMIZATION=true\n"

        return result

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_groups(course_identifier: str | int) -> str:
        """List all groups and their members for a specific course.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)

        # Get all groups in the course
        groups = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'groups'), {"per_page": 100}
        )

        if isinstance(groups, dict) and "error" in groups:
            return f"Error fetching groups: {groups['error']}"

        if not groups:
            return f"No groups found for course {course_identifier}."

        # Format the output
        course_display = await get_course_code(course_id) or course_identifier
        output = f"Groups for Course {course_display}:\n\n"

        for group in groups:
            group_id = group.get("id")
            group_name = group.get("name") or "Unnamed group"
            group_category = group.get("group_category_id", "Uncategorized")
            member_count = group.get("members_count", 0)

            # Group names (self-signup) and member names/emails are
            # author-controlled (issue 239).
            output += f"Group: {fence_untrusted_inline(group_name, 'group name')}\n"
            output += f"ID: {group_id}\n"
            output += f"Category ID: {group_category}\n"
            output += f"Member Count: {member_count}\n"

            # Get members for this group
            members = await fetch_all_paginated_results(
                canvas_path('groups', group_id, 'users'), {"per_page": 100}
            )

            if isinstance(members, dict) and "error" in members:
                output += f"Error fetching members: {members['error']}\n"
            elif not members:
                output += "No members in this group.\n"
            else:
                # Anonymization happens at the client layer (core/client.py) per
                # ENABLE_DATA_ANONYMIZATION (#179)
                output += "Members:\n"
                for member in members:
                    member_id = member.get("id")
                    # `or fallback` (not the get default) — Canvas sends an
                    # explicit null email for accounts with no visible address.
                    member_name = member.get("name") or "Unnamed user"
                    member_email = member.get("email") or "No email"
                    output += (
                        f"  - {fence_untrusted_inline(member_name, 'user name')} "
                        f"(ID: {member_id}, Email: {fence_untrusted_inline(member_email, 'user email')})\n"
                    )

            output += "\n"

        return output

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_users(course_identifier: str) -> str:
        """List users enrolled in a specific course.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)

        params = {
            "include[]": ["enrollments", "email"],
            "per_page": 100
        }

        users = await fetch_all_paginated_results(canvas_path('courses', course_id, 'users'), params)

        if isinstance(users, dict) and "error" in users:
            return f"Error fetching users: {users['error']}"

        if not users:
            return f"No users found for course {course_identifier}."

        users_info = []
        for user in users:
            user_id = user.get("id")
            # `or fallback` — Canvas sends an explicit null email for accounts
            # with no visible address; the get default only covers a missing key.
            name = user.get("name") or "Unknown"
            email = user.get("email") or "No email"

            # Get enrollment info
            enrollments = user.get("enrollments", [])
            roles = [enrollment.get("role", "Student") for enrollment in enrollments]
            role_list = ", ".join(set(roles)) if roles else "Student"

            # Display names and emails are author-controlled (issue 239).
            users_info.append(
                f"ID: {user_id}\n"
                f"Name: {fence_untrusted_inline(name, 'user name')}\n"
                f"Email: {fence_untrusted_inline(email, 'user email')}\n"
                f"Roles: {role_list}\n"
            )

        course_display = await get_course_code(course_id) or course_identifier
        return f"Users in Course {course_display}:\n\n" + "\n".join(users_info)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_student_analytics(course_identifier: str,
                                  include_participation: bool = True,
                                  include_assignment_stats: bool = True,
                                  include_access_stats: bool = True,
                                  sort_by: str = "engagement_score") -> str:
        """Get per-student engagement analytics: page views, participations, and on-time/late/missing assignment counts.

        Uses Canvas's /analytics/student_summaries endpoint to return a ranked table of every
        student in the course with an engagement score (0-100) useful for identifying disengaged
        students in participation/presentation-driven courses.

        Args:
            course_identifier: Course code or Canvas ID
            include_participation: Include participation counts (default: True)
            include_assignment_stats: Include on-time/late/missing counts (default: True)
            include_access_stats: Include page view counts (default: True)
            sort_by: Sort order — "engagement_score" (default, ascending), "page_views", "participations", or "name"
        """
        course_id = await get_course_id(course_identifier)

        course_response = await make_canvas_request("get", canvas_path('courses', course_id))
        if "error" in course_response:
            return f"Error fetching course: {course_response['error']}"
        course_name = course_response.get("name", "Unknown Course")

        # Real per-student analytics endpoint
        summaries = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'analytics', 'student_summaries'),
            {"per_page": 100}
        )
        if isinstance(summaries, dict) and "error" in summaries:
            return f"Error fetching student summaries: {summaries['error']}"

        # Student roster for names
        students = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'users'),
            {"enrollment_type[]": "student", "per_page": 100}
        )
        if isinstance(students, dict) and "error" in students:
            students = []

        by_id = {u.get("id"): u for u in students}

        rows = []
        for s in summaries:
            uid = s.get("id")
            u = by_id.get(uid, {})
            tb = s.get("tardiness_breakdown") or {}
            pv = s.get("page_views") or 0
            max_pv = s.get("max_page_views") or 0
            part = s.get("participations") or 0
            max_part = s.get("max_participations") or 0
            on_time = tb.get("on_time", 0)
            late = tb.get("late", 0)
            missing = tb.get("missing", 0)
            total = tb.get("total", 0) or 0
            submitted = on_time + late

            pv_pct = round((pv / max_pv) * 100) if max_pv else 0
            part_pct = round((part / max_part) * 100) if max_part else 0
            submit_rate = round((submitted / total) * 100) if total else 0
            score = round(0.4 * pv_pct + 0.4 * part_pct + 0.2 * submit_rate)

            rows.append({
                "name": u.get("sortable_name") or u.get("name") or f"user_{uid}",
                "user_id": uid,
                "page_views": pv,
                "page_views_pct_of_max": pv_pct,
                "participations": part,
                "participations_pct_of_max": part_pct,
                "on_time": on_time,
                "late": late,
                "missing": missing,
                "total_assignments": total,
                "submit_rate_pct": submit_rate,
                "engagement_score": score,
            })

        if sort_by == "page_views":
            rows.sort(key=lambda r: r["page_views"])
        elif sort_by == "participations":
            rows.sort(key=lambda r: r["participations"])
        elif sort_by == "name":
            rows.sort(key=lambda r: r["name"].lower())
        else:
            rows.sort(key=lambda r: r["engagement_score"])

        course_display = await get_course_code(course_id) or course_identifier
        lines = [
            f"Student Engagement Analytics — {course_display} ({course_name})",
            f"Students: {len(rows)} | Sorted by: {sort_by}",
            "",
            "Engagement score = 0.4 * page_views_%_of_max + 0.4 * participations_%_of_max + 0.2 * submit_rate_%",
            "",
        ]

        header_parts = ["Name", "Score"]
        if include_access_stats:
            header_parts += ["PageViews", "PV%"]
        if include_participation:
            header_parts += ["Parts", "Part%"]
        if include_assignment_stats:
            header_parts += ["OnTime", "Late", "Missing", "Total"]
        lines.append(" | ".join(header_parts))
        lines.append("-" * 100)

        for r in rows:
            # Student names are author-controlled (issue 239). Truncate the RAW
            # name first, THEN fence — slicing a fenced string would split the
            # marker. The fence makes the name column variable-width, so the
            # fixed-column alignment no longer applies to it.
            safe_name = fence_untrusted_inline(str(r["name"])[:30], "student name")
            parts = [safe_name, str(r["engagement_score"]).rjust(3)]
            if include_access_stats:
                parts += [str(r["page_views"]).rjust(5), f"{r['page_views_pct_of_max']}%".rjust(4)]
            if include_participation:
                parts += [str(r["participations"]).rjust(4), f"{r['participations_pct_of_max']}%".rjust(4)]
            if include_assignment_stats:
                parts += [
                    str(r["on_time"]).rjust(3),
                    str(r["late"]).rjust(3),
                    str(r["missing"]).rjust(3),
                    str(r["total_assignments"]).rjust(3),
                ]
            lines.append(" | ".join(parts))

        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_student_anonymization_map(
        course_identifier: str | int, save_directory: str = "local_maps"
    ) -> str:
        """Create a private local identity-map bundle without overwriting prior maps.

        Args:
            course_identifier: Course code or Canvas ID
            save_directory: Local destination; existing directories require mode 0700.
                Each call creates a new bundle with raw JSON, spreadsheet-safe CSV,
                and a completion manifest. Available only through local stdio.
        """
        import csv
        import hashlib
        import io
        import json
        from datetime import UTC, datetime
        from urllib.parse import urlsplit

        from ..core.anonymization import generate_anonymous_id
        from ..core.config import get_config
        from ..core.local_artifacts import write_private_bundle

        if is_http_request_active():
            return (
                "Error: Creating a local identity map is only available on a local "
                "(stdio) server. No file was written."
            )

        try:
            course_id = str(await get_course_id(course_identifier))
            if not course_id.isascii() or not course_id.isdecimal():
                course = await make_canvas_request("get", canvas_path("courses", course_id))
                if not isinstance(course, dict) or "error" in course:
                    return "Error: Could not resolve the canonical course ID. No file was written."
                course_id = str(course.get("id", ""))
            if not course_id.isascii() or not course_id.isdecimal() or int(course_id) <= 0:
                return "Error: Invalid canonical course ID. No file was written."
            course_id = str(int(course_id))
            parsed = urlsplit(get_config().canvas_api_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "Error: Invalid Canvas origin. No file was written."
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            port = parsed.port
            suffix = f":{port}" if port and port != {"http": 80, "https": 443}[parsed.scheme] else ""
            origin = f"{parsed.scheme}://{host}{suffix}"
            students = await fetch_all_paginated_results(
                canvas_path("courses", course_id, "users"),
                {"enrollment_type[]": "student", "include[]": ["email"], "per_page": 100},
                skip_anonymization=True,
            )
            if not isinstance(students, list):
                return "Error: Could not fetch a complete roster. No file was written."
            if not students:
                return "No students found. No file was written."
            records = []
            seen_ids: set[int] = set()
            seen_pseudonyms: set[str] = set()
            for student in students:
                if not isinstance(student, dict):
                    raise ValueError("Invalid roster record")
                real_id = student.get("id")
                if type(real_id) is not int or real_id <= 0 or real_id in seen_ids:
                    raise ValueError("Invalid or duplicate student ID")
                name, email = student.get("name"), student.get("email")
                if (name is not None and not isinstance(name, str)) or (
                    email is not None and not isinstance(email, str)
                ):
                    raise ValueError("Invalid identity fields")
                anonymous_id = generate_anonymous_id(real_id, prefix="Student")
                expected_id = "Student_" + hashlib.sha256(str(real_id).encode()).hexdigest()[:8]
                if anonymous_id != expected_id:
                    return (
                        "Error: Cached pseudonyms do not match the declared map algorithm. "
                        "No file was written; existing pseudonyms were not changed."
                    )
                if anonymous_id in seen_pseudonyms:
                    raise ValueError("Pseudonym collision")
                seen_ids.add(real_id)
                seen_pseudonyms.add(anonymous_id)
                records.append({
                    "real_name": name, "real_id": real_id,
                    "real_email": email, "anonymous_id": anonymous_id,
                })
            output = io.StringIO(newline="")
            writer = csv.DictWriter(
                output, fieldnames=["real_name", "real_id", "real_email", "anonymous_id"]
            )
            writer.writeheader()
            writer.writerows({
                **record,
                "real_name": csv_safe_cell(record["real_name"] or ""),
                "real_email": csv_safe_cell(record["real_email"] or ""),
            } for record in records)
            origin_hash = hashlib.sha256(origin.encode()).hexdigest()[:16]
            bundle = write_private_bundle(
                save_directory, f"canvas-{origin_hash}-course-{course_id}-map-v1",
                {
                    "identities.json": json.dumps(records, ensure_ascii=False).encode(),
                    "anonymization_map.csv": output.getvalue().encode(),
                },
                {
                    "schema_version": 1, "kind": "student_identity_map",
                    "canvas_origin": origin, "course_id": course_id,
                    "pseudonym_algorithm": "sha256-id8-student-v1",
                    "created_at": datetime.now(UTC).isoformat(),
                    "record_count": len(records),
                },
            )
            return (
                f"Student anonymization map created successfully.\n"
                f"File location: {bundle / 'anonymization_map.csv'}\n"
                f"Completion manifest: {bundle / 'manifest.json'}\n"
                f"Students mapped: {len(records)}\n"
                "Private identity files remain local. Prior maps were not overwritten."
            )
        except Exception:
            return (
                "Error: Identity-map completion could not be confirmed. "
                "Check the private destination and available disk space. "
                "Any directory without a valid manifest is incomplete; do not use it."
            )
