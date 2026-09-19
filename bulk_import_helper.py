"""
bulk_import_helper.py - Robust parsing and normalization utilities for student bulk import.
Supports .xlsx, .xls, and .csv files with auto-header detection, float cleaning, and college mapping.
"""
import io
import re
from typing import List, Dict, Any, Optional
from database import COLLEGES

ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

def to_eng(s: Any) -> str:
    """Convert Arabic-Indic numerals to standard Western digits."""
    if s is None:
        return ""
    return str(s).translate(ARABIC_DIGITS_MAP)


COL_MAP = {
    "student_id": [
        "student_id", "studentid", "student id", "id", "الرقم",
        "رقم الطالب", "رقم_الطالب", "رقم_طالب",
        "كود الطالب", "كود_الطالب", "كود", "code", "student_code", "student code",
        "الرقم الجامعي", "الرقم_الجامعي", "university_id", "academic_id",
        "رقم الجلوس", "رقم_الجلوس", "جلوس", "seat_no", "seat_number",
        "رقم القيد", "رقم_القيد", "القيد", "رقم التسجيل", "تسجيل",
        "الرقم القومي", "الرقم_القومي", "national_id", "nid"
    ],
    "full_name": [
        "full_name", "fullname", "full name", "name", "الاسم",
        "اسم الطالب", "اسم_الطالب", "student_name", "student name",
        "الاسم بالكامل", "الاسم_بالكامل", "الاسم الكامل", "الاسم_الكامل",
        "الاسم رباعي", "الاسم الرباعي", "اسم"
    ],
    "year": [
        "year", "السنة", "العام", "سنة", "عام",
        "الفرقة", "الفرقة الدراسية", "الفرقة_الدراسية", "فرقة",
        "المستوى", "المستوى الدراسي", "المستوى_الدراسي", "مستوى",
        "السنة الدراسية", "العام الدراسي", "academic_year", "level", "grade", "study_year"
    ],
    "college": [
        "college", "الكلية", "كلية", "faculty", "school",
        "القسم", "التخصص", "البرنامج", "department", "dept", "program"
    ],
    "email": [
        "الايميل", "الإيميل", "البريد الالكتروني", "البريد الالكترونى",
        "البريد الإلكتروني", "البريد الإلكترونى", "البريد", "ايميل", "إيميل",
        "email", "e-mail", "mail", "email address",
        "بريد الطالب", "ايميل الطالب", "إيميل الطالب", "البريد الجامعي", "الايميل الجامعي"
    ],
}


def normalize_header(s: Any) -> str:
    """Normalize Arabic and English header strings for resilient matching."""
    if not s:
        return ""
    s = str(s).strip().lower().replace("_", " ")
    # Unify Alef forms: إ, أ, آ -> ا
    s = re.sub(r"[إأآا]", "ا", s)
    # Unify Ya / Alef Maksura: ى -> ي
    s = re.sub(r"[ىي]", "ي", s)
    # Unify Ta Marbuta / Ha: ة -> ه
    s = re.sub(r"ة", "ه", s)
    # Remove Tashkeel (diacritics)
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def clean_excel_val(v: Any) -> str:
    """
    Clean cell values from openpyxl/xlrd/csv:
    - Eliminates trailing float '.0' (e.g. 2026101001.0 -> '2026101001')
    - Converts Arabic-Indic numbers to English digits
    - Strips whitespace
    """
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v)).strip()
        s = str(v).strip()
        if s.endswith(".0"):
            return s[:-2]
        return s
    if isinstance(v, int):
        return str(v).strip()
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        return s[:-2]
    return to_eng(s)


def find_col(row_dict: Dict[str, Any], aliases: List[str]) -> str:
    """Find column value in row dictionary matching any of the given aliases."""
    # 1. Direct exact key match
    for a in aliases:
        if a in row_dict:
            val = clean_excel_val(row_dict[a])
            if val:
                return val

    # 2. Normalized header match (handles 'البريد الالكترونى' vs 'الايميل' vs 'البريد الالكتروني' etc.)
    norm_aliases = [normalize_header(a) for a in aliases]
    for k, v in row_dict.items():
        norm_k = normalize_header(k)
        if not norm_k:
            continue
        for a_norm in norm_aliases:
            if a_norm == norm_k or a_norm in norm_k or norm_k in a_norm:
                val = clean_excel_val(v)
                if val:
                    return val
    return ""


def extract_email(row_dict: Dict[str, Any]) -> str:
    """
    Extract email address from row dictionary:
    1. Checks all standard aliases (الايميل, البريد الالكتروني, الإيمبيل, etc.)
    2. Fallback: inspects any column header containing 'يميل', 'يمبيل', 'بريد', or 'mail'
    3. Fallback: inspects any cell value containing '@' and '.'
    4. Auto-corrects typo where '@' was replaced by dot before domain (e.g. user.2020.bua.edu.eg)
    """
    val = find_col(row_dict, COL_MAP["email"]).strip()
    if not val:
        for k, v in row_dict.items():
            k_norm = normalize_header(k)
            if any(w in k_norm for w in ("يميل", "يمبيل", "بريد", "mail")):
                val = clean_excel_val(v).strip()
                if val:
                    break
    if not val:
        for k, v in row_dict.items():
            s = clean_excel_val(v).strip()
            if "@" in s and "." in s and " " not in s and len(s) > 5:
                val = s
                break
    if val and "@" not in val and ".bua.edu.eg" in val:
        val = val.rsplit(".bua.edu.eg", 1)[0] + "@bua.edu.eg"

    return val.lower() if val else ""


def parse_uploaded_file(raw_bytes: bytes, filename: str) -> List[Dict[str, str]]:
    """
    Parse an uploaded .xlsx, .xls, or .csv file into a list of row dictionaries.
    Dynamically detects the header row among the first 10 rows to skip titles/banners.
    Silently ignores completely empty rows.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    raw_rows: List[List[str]] = []

    if ext == "csv":
        import csv
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1", "iso-8859-6"):
            try:
                text = raw_bytes.decode(enc)
                break
            except Exception:
                continue
        if text is None:
            raise ValueError("تعذر فك ترميز ملف CSV. يرجى حفظ الملف بترميز UTF-8 أو رفع ملف Excel بصيغة .xlsx")
        reader = csv.reader(io.StringIO(text))
        for r in reader:
            raw_rows.append([clean_excel_val(c) for c in r])

    elif ext == "xls":
        try:
            import xlrd
            book = xlrd.open_workbook(file_contents=raw_bytes)
            sheet = book.sheet_by_index(0)
            for r in range(sheet.nrows):
                raw_rows.append([clean_excel_val(sheet.cell_value(r, c)) for c in range(sheet.ncols)])
        except Exception as e:
            raise ValueError(f"تعذر قراءة ملف Excel (.xls): {e}")

    else:  # default .xlsx
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            try:
                ws = wb.active
                for xl_row in ws.iter_rows(values_only=True):
                    raw_rows.append([clean_excel_val(v) for v in xl_row])
            finally:
                wb.close()
        except Exception as e:
            raise ValueError(f"تعذر قراءة ملف Excel (.xlsx): {e}")

    if not raw_rows:
        return []

    # Build set of all known header keywords (normalized)
    all_keywords = set()
    for aliases in COL_MAP.values():
        for a in aliases:
            all_keywords.add(normalize_header(a))

    # Detect header row among the first 10 rows
    best_header_idx = 0
    max_matches = 0

    for idx, r in enumerate(raw_rows[:10]):
        matches = 0
        for cell in r:
            norm_cell = normalize_header(cell)
            if not norm_cell:
                continue
            for kw in all_keywords:
                if kw == norm_cell or kw in norm_cell or norm_cell in kw:
                    matches += 1
                    break
        if matches > max_matches:
            max_matches = matches
            best_header_idx = idx

    headers = [str(c).strip() for c in raw_rows[best_header_idx]]

    rows: List[Dict[str, str]] = []
    for r in raw_rows[best_header_idx + 1:]:
        # Skip completely empty rows
        if not any(str(c).strip() for c in r):
            continue
        row_dict: Dict[str, str] = {}
        for h, v in zip(headers, r):
            if h:
                row_dict[h] = str(v).strip()
        rows.append(row_dict)

    return rows


def normalize_arabic(text: str) -> str:
    """Normalize Arabic characters for fuzzy college matching."""
    if not text:
        return ""
    text = re.sub(r"[إأآا]", "ا", text)
    text = re.sub(r"ة", "ه", text)
    text = re.sub(r"ى", "ي", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def match_college_name(college_input: str, user_role: Optional[str] = None, user_college: Optional[str] = None) -> str:
    """
    Match arbitrary college strings from Excel to standard COLLEGES list.
    Enforces user_college if user is college admin.
    """
    if user_role == "admin" and user_college:
        return user_college

    if not college_input:
        return COLLEGES[0]

    college_str = str(college_input).strip()
    if college_str in COLLEGES:
        return college_str

    norm_input = normalize_arabic(college_str)

    # 1. Direct normalized exact match
    for c in COLLEGES:
        if normalize_arabic(c) == norm_input:
            return c

    # 2. Domain keywords mapping
    mapping = [
        (["اسنان", "dentist", "dental"], "كلية طب الأسنان"),
        (["اكلينيك", "كلينيكال", "clinical"], "كلية صيدلة اكلينيكية"),
        (["صيدل", "فارما", "pharmacy", "pharma"], "كلية الصيدلة فارما D"),
        (["علاج طبيعي", "طبيعي", "physiotherapy", "physical therapy"], "كلية العلاج الطبيعي"),
        (["بيطر", "veterinary", "vet"], "كلية الطب البيطري"),
        (["حيويه", "بيوتكنولوجي", "biotech"], "كلية تكنلوجيا علوم حيوية"),
        (["علوم صحيه", "صحيه تطبيقيه", "applied health"], "كلية العلوم الصحية التطبيقية"),
        (["تمريض", "nursing", "nurse"], "كلية التمريض"),
        (["ذكاء", "اصطناعي", "حاسب", "معلومات", "بيانات", "ai", "computer", "cs", "it"], "كلية  ذكاء اصطناعي وعلوم البيانات"),
        (["بزنس", "اداره", "اعمال", "تجاره", "تسويق", "محاسبه", "business", "commerce"], "كلية بزنس وإدارة الأعمال"),
        (["لغات", "ترجمه", "السن", "languages"], "كلية لغات وترجمة"),
        (["حقوق", "شريعه", "قانون", "law"], "كلية الحقوق"),
        (["فنون", "جميله", "تطبيقيه", "arts", "fine arts"], "كلية الفنون الجميلة"),
    ]

    for keywords, official in mapping:
        for kw in keywords:
            if kw in norm_input:
                return official

    # 3. Partial match against official names
    for c in COLLEGES:
        norm_c = normalize_arabic(c).replace("كليه", "").strip()
        clean_in = norm_input.replace("كليه", "").strip()
        if norm_c and (norm_c in clean_in or clean_in in norm_c):
            return c

    return COLLEGES[0]


def extract_academic_year(year_val: str, sid: str, current_year: int) -> str:
    """Extract a 4-digit academic year from Excel cell, student_id, or fallback."""
    y_clean = to_eng(str(year_val or "")).strip()
    m = re.search(r"\b(20\d{2})\b", y_clean)
    if m:
        return m.group(1)

    # Check if sid begins with a valid 4-digit year (e.g. 2024001001)
    if sid and len(sid) >= 4 and sid[:4].isdigit() and 2000 <= int(sid[:4]) <= current_year + 5:
        return sid[:4]

    return str(current_year)
