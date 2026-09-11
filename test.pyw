#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

APP_NAME = "Filament Profile Tool"
APP_CONFIG_PATH = Path.home() / ".filament_profile_tool.json"
SCRIPT_DIR = Path(__file__).resolve().parent
FALLBACK_ORCA_VERSION = "1.9.0.0"   # χαμηλή έκδοση = πάντα αποδεκτή από το Orca
INVALID_NAME_CHARS = set('<>:"/\\|?*')
HISTORY_DIR = ".history"
HISTORY_KEEP = 20

FILAMENT_TYPES = ["PLA", "PLA-CF", "PETG", "PETG-CF", "PCTG", "ABS", "ASA", "TPU",
                  "PA", "PA-CF", "PC", "PET-CF", "HIPS", "PVA", "PP", "PPS"]


# --------------------------------------------------------------------------- #
#  Ορισμός πεδίων (τα key είναι τα ονόματα ρυθμίσεων του OrcaSlicer)
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    key: str
    label: str
    kind: str = "num"          # text | num | int | choice | color | bool | notes
    unit: str = ""
    options: tuple = ()


SECTIONS = [
    ("Γενικά", [
        Field("filament_vendor", "Κατασκευαστής", "text"),
        Field("filament_type", "Τύπος υλικού", "choice", options=tuple(FILAMENT_TYPES)),
        Field("filament_colour", "Χρώμα", "color"),
        Field("filament_diameter", "Διάμετρος", "num", "mm"),
        Field("filament_density", "Πυκνότητα", "num", "g/cm³"),
        Field("filament_cost", "Κόστος", "num", "ανά kg"),
    ], ""),
    ("Θερμοκρασίες", [
        Field("nozzle_temperature_initial_layer", "Ακροφύσιο – 1η στρώση", "int", "°C"),
        Field("nozzle_temperature", "Ακροφύσιο – υπόλοιπες στρώσεις", "int", "°C"),
        Field("nozzle_temperature_range_low", "Προτεινόμενο εύρος – ελάχιστο", "int", "°C"),
        Field("nozzle_temperature_range_high", "Προτεινόμενο εύρος – μέγιστο", "int", "°C"),
        Field("hot_plate_temp_initial_layer", "Smooth PEI / High Temp – 1η στρώση", "int", "°C"),
        Field("hot_plate_temp", "Smooth PEI / High Temp – υπόλοιπες", "int", "°C"),
        Field("textured_plate_temp_initial_layer", "Textured PEI – 1η στρώση", "int", "°C"),
        Field("textured_plate_temp", "Textured PEI – υπόλοιπες", "int", "°C"),
        Field("cool_plate_temp_initial_layer", "Cool plate – 1η στρώση", "int", "°C"),
        Field("cool_plate_temp", "Cool plate – υπόλοιπες", "int", "°C"),
    ], ""),
    ("Ροή & πίεση", [
        Field("filament_flow_ratio", "Flow ratio", "num"),
        Field("filament_max_volumetric_speed", "Μέγιστη ογκομετρική ταχύτητα", "num", "mm³/s"),
        Field("enable_pressure_advance", "Pressure advance ενεργό", "bool"),
        Field("pressure_advance", "Pressure advance (K)", "num"),
    ], "Η τιμή K εφαρμόζεται μόνο όταν το pressure advance είναι ενεργό."),
    ("Ψύξη", [
        Field("fan_min_speed", "Ανεμιστήρας – ελάχιστη ταχύτητα", "int", "%"),
        Field("fan_max_speed", "Ανεμιστήρας – μέγιστη ταχύτητα", "int", "%"),
        Field("close_fan_the_first_x_layers", "Χωρίς ανεμιστήρα για τις πρώτες", "int", "στρώσεις"),
        Field("overhang_fan_speed", "Ανεμιστήρας σε προεξοχές", "int", "%"),
        Field("additional_cooling_fan_speed", "Βοηθητικός ανεμιστήρας", "int", "%"),
        Field("slow_down_layer_time", "Ελάχιστος χρόνος στρώσης", "num", "s"),
    ], ""),
    ("Ανάσυρση", [
        Field("filament_retraction_length", "Μήκος ανάσυρσης", "num", "mm"),
        Field("filament_retraction_speed", "Ταχύτητα ανάσυρσης", "num", "mm/s"),
        Field("filament_deretraction_speed", "Ταχύτητα επαναφοράς", "num", "mm/s"),
        Field("filament_z_hop", "Z hop", "num", "mm"),
    ], "Κενό πεδίο = χρησιμοποιούνται οι τιμές ανάσυρσης του εκτυπωτή."),
    ("Σημειώσεις", [
        Field("filament_notes", "Σημειώσεις", "notes"),
    ], "Οι σημειώσεις γράφονται και μέσα στο preset του OrcaSlicer."),
]

ALL_FIELDS = [f for _, fields, _ in SECTIONS for f in fields]
FIELD_BY_KEY = {f.key: f for f in ALL_FIELDS}

# Κλειδιά του Orca που διαχειρίζεται το ίδιο το εργαλείο (δεν μπαίνουν στο "extra")
ORCA_META_KEYS = {"type", "name", "inherits", "from", "version",
                  "filament_settings_id", "instantiation", "setting_id", "base_id",
                  "user_id", "updated_time", "renamed_from"}


# --------------------------------------------------------------------------- #
#  Βοηθητικά
# --------------------------------------------------------------------------- #
def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def atomic_write_json(path: Path, data) -> None:
    """Γράφει πρώτα σε προσωρινό αρχείο και μετά το αντικαθιστά (ασφαλές σε κοινόχρηστους φακέλους)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def parse_version(text: str):
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts) if parts else None


def normalize_value(field: Field, value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if field.kind in ("num", "int"):
        value = value.replace(",", ".")
    elif field.kind == "color":
        value = value.upper()
        if re.fullmatch(r"[0-9A-F]{6}", value):
            value = "#" + value
    elif field.kind == "bool":
        value = {"true": "1", "false": "0"}.get(value.lower(), value)
    return value


def new_profile(name: str, inherits: str = "") -> dict:
    return {"schema": 1, "name": name, "inherits": inherits,
            "settings": {}, "extra": {}, "updated": "", "updated_by": ""}


def normalize_profile(data: dict, fallback_name: str = "") -> dict:
    p = new_profile(data.get("name") or fallback_name, data.get("inherits", ""))
    p["settings"] = {k: str(v) for k, v in (data.get("settings") or {}).items() if k in FIELD_BY_KEY}
    p["extra"] = dict(data.get("extra") or {})
    p["updated"] = data.get("updated", "")
    p["updated_by"] = data.get("updated_by", "")
    return p


def validate_profile(p: dict, known_parents: set | None = None):
    """Επιστρέφει (errors, warnings)."""
    errors, warnings = [], []
    name = p["name"]
    if not name:
        errors.append("Το όνομα είναι υποχρεωτικό.")
    bad = sorted({c for c in name if c in INVALID_NAME_CHARS})
    if bad:
        errors.append("Το όνομα δεν μπορεί να περιέχει: " + " ".join(bad))
    for key, val in p["settings"].items():
        f = FIELD_BY_KEY.get(key)
        if not f or not val:
            continue
        if f.kind in ("num", "int"):
            try:
                float(val)
            except ValueError:
                errors.append(f"{f.label}: «{val}» δεν είναι αριθμός.")
        elif f.kind == "color" and not re.fullmatch(r"#[0-9A-F]{6}([0-9A-F]{2})?", val):
            errors.append(f"{f.label}: το χρώμα πρέπει να είναι της μορφής #RRGGBB.")
    if not p["inherits"] and "@" in name:
        warnings.append("Το όνομα περιέχει «@» και δεν έχει βασικό προφίλ. Το OrcaSlicer θα "
                        "θεωρήσει ό,τι ακολουθεί το «@» ως όνομα εκτυπωτή και θα το δείχνει μόνο σε αυτόν.")
    if p["inherits"] and known_parents and p["inherits"] not in known_parents:
        warnings.append(f"Το βασικό προφίλ «{p['inherits']}» δεν βρέθηκε σε αυτή την εγκατάσταση "
                        "του OrcaSlicer. Το Orca αγνοεί presets με ανύπαρκτο βασικό προφίλ.")
    return errors, warnings


# --------------------------------------------------------------------------- #
#  Βιβλιοθήκη (filesystem)
# --------------------------------------------------------------------------- #
class Library:
    def __init__(self, root):
        self.root = Path(root)

    def path_for(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def list_profiles(self) -> list:
        out = []
        if not self.root.is_dir():
            return out
        for f in sorted(self.root.glob("*.json"), key=lambda x: x.name.lower()):
            try:
                out.append(normalize_profile(json.loads(f.read_text(encoding="utf-8")), f.stem))
            except Exception as e:
                print(f"Παράλειψη κατεστραμμένου αρχείου {f.name}: {e}", file=sys.stderr)
        return out

    def load(self, name: str) -> dict:
        return normalize_profile(json.loads(self.path_for(name).read_text(encoding="utf-8")), name)

    def save(self, profile: dict, old_name: str | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        profile["updated"] = datetime.now().isoformat(timespec="seconds")
        profile["updated_by"] = current_user()
        target = self.path_for(profile["name"])
        if target.exists():
            self._backup(target)
        atomic_write_json(target, profile)
        if old_name and old_name != profile["name"]:
            old = self.path_for(old_name)
            if old.exists():
                self._backup(old)
                old.unlink()

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if path.exists():
            self._backup(path)
            path.unlink()

    def _backup(self, path: Path) -> None:
        hist = self.root / HISTORY_DIR / path.stem
        hist.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, hist / f"{datetime.now():%Y%m%d-%H%M%S}.json")
        for old in sorted(hist.glob("*.json"))[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
#  OrcaSlicer
# --------------------------------------------------------------------------- #
def candidate_orca_dirs() -> list:
    home = Path.home()
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "OrcaSlicer"]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "OrcaSlicer"]
    return [home / ".config" / "OrcaSlicer",
            home / ".var" / "app" / "io.github.softfever.OrcaSlicer" / "config" / "OrcaSlicer"]


def detect_orca_dir() -> str:
    for d in candidate_orca_dirs():
        if (d / "user").is_dir() or (d / "system").is_dir():
            return str(d)
    return ""


def list_orca_users(orca_dir: str) -> list:
    base = Path(orca_dir) / "user"
    users = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if "default" not in users:
        users.insert(0, "default")
    return users


def detect_orca_user(orca_dir: str) -> str:
    base = Path(orca_dir) / "user"
    if not base.is_dir():
        return "default"
    dirs = [p for p in base.iterdir() if p.is_dir()]
    if not dirs:
        return "default"
    return max(dirs, key=lambda p: p.stat().st_mtime).name   # ο πιο πρόσφατα ενεργός χρήστης


def orca_filament_dir(orca_dir: str, user: str) -> Path:
    return Path(orca_dir) / "user" / (user or "default") / "filament"


def detect_orca_exe() -> str:
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        for r in roots:
            p = Path(r) / "OrcaSlicer" / "orca-slicer.exe"
            if p.exists():
                return str(p)
        return ""
    if sys.platform == "darwin":
        for p in (Path("/Applications/OrcaSlicer.app"), Path.home() / "Applications" / "OrcaSlicer.app"):
            if p.exists():
                return str(p)
        return ""
    for name in ("orca-slicer", "OrcaSlicer", "orcaslicer"):
        found = shutil.which(name)
        if found:
            return found
    if (Path.home() / ".var" / "app" / "io.github.softfever.OrcaSlicer").is_dir():
        return "flatpak run io.github.softfever.OrcaSlicer"
    return ""


def scan_system_filaments(orca_dir: str):
    """Επιστρέφει (ονόματα για τη λίστα, σύνολο έγκυρων ονομάτων γονέα)."""
    names, valid = set(), set()
    sysdir = Path(orca_dir) / "system"
    if not sysdir.is_dir():
        return [], set()
    for vendor in sysdir.iterdir():
        fdir = vendor / "filament"
        if not fdir.is_dir():
            continue
        for f in fdir.rglob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("instantiation", "true")).lower() != "true":
                continue
            if data.get("name"):
                names.add(data["name"])
                valid.add(data["name"])
            if data.get("renamed_from"):
                valid.add(data["renamed_from"])
    return sorted(names, key=str.lower), valid


def detect_orca_version(orca_dir: str) -> str:
    """Παίρνει το version από υπάρχοντα user presets, ώστε τα αρχεία μας να μοιάζουν με του Orca."""
    best = None
    base = Path(orca_dir) / "user"
    if base.is_dir():
        for f in list(base.glob("*/*/*.json"))[:300]:
            try:
                v = json.loads(f.read_text(encoding="utf-8")).get("version", "")
            except Exception:
                continue
            pv = parse_version(v)
            if pv and len(pv) == 4 and (best is None or pv > best[0]):
                best = (pv, v)
    return best[1] if best else FALLBACK_ORCA_VERSION


def profile_to_orca(p: dict, version: str) -> dict:
    out = {"type": "filament", "name": p["name"], "inherits": p["inherits"], "from": "User",
           "is_custom_defined": "0", "version": version, "filament_settings_id": [p["name"]]}
    for k, v in p.get("extra", {}).items():
        if k not in ORCA_META_KEYS:
            out[k] = v
    for k, v in p["settings"].items():
        if str(v).strip() != "":
            out[k] = [str(v).strip()]
    return out


def orca_to_profile(data: dict, name: str) -> dict:
    p = new_profile(data.get("name") or name, data.get("inherits", "") or "")
    for k, v in data.items():
        if k in ORCA_META_KEYS:
            continue
        if k in FIELD_BY_KEY:
            val = v[0] if isinstance(v, list) and v else ("" if isinstance(v, list) else v)
            val = "" if str(val) == "nil" else normalize_value(FIELD_BY_KEY[k], str(val))
            if val:
                p["settings"][k] = val
        else:
            p["extra"][k] = v
    return p


def export_to_orca(p: dict, fdir: Path, version: str) -> Path:
    fdir.mkdir(parents=True, exist_ok=True)
    target = fdir / f"{p['name']}.json"
    atomic_write_json(target, profile_to_orca(p, version))
    # Το .info το χρησιμοποιεί το Orca για cloud sync. Αν το preset έχει ήδη συγχρονιστεί,
    # το σημαδεύουμε "update" ώστε το Orca να ανεβάσει τη δική μας έκδοση αντί να την πατήσει.
    info = target.with_suffix(".info")
    fields = {"sync_info": "", "user_id": "", "setting_id": "", "base_id": "", "updated_time": ""}
    if info.exists():
        for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        if fields.get("setting_id") and fields.get("sync_info") in ("", "hold"):
            fields["sync_info"] = "update"
    fields["updated_time"] = str(int(time.time()))
    info.write_text("".join(f"{k} = {fields.get(k, '')}\n" for k in
                            ("sync_info", "user_id", "setting_id", "base_id", "updated_time")),
                    encoding="utf-8")
    return target


def read_orca_user_presets(fdir: Path) -> list:
    out = []
    if not fdir.is_dir():
        return out
    for f in sorted(fdir.glob("*.json"), key=lambda x: x.name.lower()):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("type", "filament") == "filament":
            out.append(orca_to_profile(data, f.stem))
    return out


def is_orca_running() -> bool:
    try:
        if sys.platform == "win32":
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq orca-slicer.exe", "/NH"],
                               capture_output=True, text=True, creationflags=0x08000000)
            return "orca-slicer.exe" in r.stdout.lower()
        # ταίριασμα μόνο στο όνομα της διεργασίας (όχι σε όλη τη γραμμή εντολών)
        r = subprocess.run(["pgrep", "-i", "orca.?slicer"], capture_output=True, text=True)
        return any(x.isdigit() and int(x) != os.getpid() for x in r.stdout.split())
    except Exception:
        return False


def launch_orca(exe: str) -> str | None:
    """Ανοίγει το OrcaSlicer. Επιστρέφει μήνυμα σφάλματος ή None."""
    try:
        if sys.platform == "darwin" and (not exe or exe.endswith(".app")):
            subprocess.Popen(["open", exe or "-a", *([] if exe else ["OrcaSlicer"])])
            return None
        if not exe:
            return "Δεν έχει οριστεί το πρόγραμμα του OrcaSlicer. Όρισέ το στις Ρυθμίσεις."
        cmd = [exe] if Path(exe).exists() else shlex.split(exe)
        kwargs = {"close_fds": True}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return None
    except OSError as e:
        return f"Το OrcaSlicer δεν άνοιξε: {e}"


# --------------------------------------------------------------------------- #
#  Ρυθμίσεις εφαρμογής
# --------------------------------------------------------------------------- #
def default_library_dir() -> str:
    beside = SCRIPT_DIR / "FilamentLibrary"   # π.χ. όταν το εργαλείο τρέχει από τον server
    return str(beside if beside.is_dir() else Path.home() / "FilamentLibrary")


def load_config() -> dict:
    cfg = {"library_dir": "", "orca_dir": "", "orca_user": "", "orca_exe": ""}
    if APP_CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    cfg["library_dir"] = cfg["library_dir"] or default_library_dir()
    cfg["orca_dir"] = cfg["orca_dir"] or detect_orca_dir()
    cfg["orca_user"] = cfg["orca_user"] or (detect_orca_user(cfg["orca_dir"]) if cfg["orca_dir"] else "default")
    cfg["orca_exe"] = cfg["orca_exe"] or detect_orca_exe()
    return cfg


def save_config(cfg: dict) -> None:
    atomic_write_json(APP_CONFIG_PATH, cfg)


# --------------------------------------------------------------------------- #
#  Γραμμή εντολών
# --------------------------------------------------------------------------- #
def cli_sync(launch: bool) -> int:
    cfg = load_config()
    if not cfg["orca_dir"]:
        print("Δεν βρέθηκε ο φάκελος του OrcaSlicer. Άνοιξε το UI και όρισέ τον στις Ρυθμίσεις.")
        return 1
    fdir = orca_filament_dir(cfg["orca_dir"], cfg["orca_user"])
    version = detect_orca_version(cfg["orca_dir"])
    _, valid = scan_system_filaments(cfg["orca_dir"])
    sent = 0
    for p in Library(cfg["library_dir"]).list_profiles():
        errors, _ = validate_profile(p)
        if errors or (p["inherits"] and valid and p["inherits"] not in valid):
            print(f"Παράλειψη «{p['name']}»: {'; '.join(errors) or 'άγνωστο βασικό προφίλ'}")
            continue
        export_to_orca(p, fdir, version)
        sent += 1
    print(f"Στάλθηκαν {sent} προφίλ στο {fdir}")
    if launch:
        if is_orca_running():
            print("Το OrcaSlicer τρέχει ήδη – κάνε επανεκκίνηση για να φορτώσει τις αλλαγές.")
        else:
            err = launch_orca(cfg["orca_exe"])
            if err:
                print(err)
                return 1
    return 0


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, colorchooser

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    BOOL_LABELS = {"": "από βασικό προφίλ", "1": "Ναι", "0": "Όχι"}
    BOOL_VALUES = {v: k for k, v in BOOL_LABELS.items()}

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_NAME)
            self.geometry("1150x720")
            self.minsize(940, 580)
            style = ttk.Style(self)
            if sys.platform.startswith("linux"):
                style.theme_use("clam")
            style.configure("Hint.TLabel", foreground="#666666")
            style.configure("Title.TLabel", font=("TkDefaultFont", 13, "bold"))

            self.cfg = load_config()
            self.library = Library(self.cfg["library_dir"])
            self.profiles: dict = {}
            self.iid_to_name: dict = {}
            self.current_name: str | None = None
            self.current_base: dict = new_profile("")
            self.snapshot = None
            self.system_names: list = []
            self.valid_parents: set = set()
            self._swatches: dict = {}
            self._suppress_select = False
            self._loading = False
            self._scan_queue: queue.Queue = queue.Queue()

            self.vars: dict = {}
            self._build_ui()
            self.reload_library()
            self.start_system_scan()
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.bind_all("<Control-s>", lambda e: self.save_current())
            self.select_first_or_new()

        # ---------------- κατασκευή UI ----------------
        def _build_ui(self):
            bar = ttk.Frame(self, padding=(10, 8))
            bar.pack(fill="x")
            ttk.Button(bar, text="Αποστολή στο Orca", command=self.export_selected).pack(side="left")
            ttk.Button(bar, text="Αποστολή όλων στο Orca", command=self.export_all).pack(side="left", padx=6)
            ttk.Button(bar, text="Εισαγωγή από Orca…", command=self.import_from_orca).pack(side="left")
            ttk.Button(bar, text="Άνοιγμα OrcaSlicer", command=self.on_launch).pack(side="left", padx=6)
            ttk.Button(bar, text="Ρυθμίσεις…", command=self.open_settings).pack(side="right")
            ttk.Button(bar, text="Φάκελος βιβλιοθήκης", command=self.open_library_folder).pack(side="right", padx=6)

            paned = ttk.PanedWindow(self, orient="horizontal")
            paned.pack(fill="both", expand=True, padx=10)
            left = ttk.Frame(paned, padding=(0, 0, 10, 0))
            right = ttk.Frame(paned)
            paned.add(left, weight=1)
            paned.add(right, weight=3)

            # --- αριστερά: λίστα ---
            self.search_var = tk.StringVar()
            self.search_var.trace_add("write", lambda *_: self.render_list())
            ttk.Label(left, text="Αναζήτηση").pack(anchor="w")
            ttk.Entry(left, textvariable=self.search_var).pack(fill="x", pady=(2, 6))
            tree_frame = ttk.Frame(left)
            tree_frame.pack(fill="both", expand=True)
            self.tree = ttk.Treeview(tree_frame, columns=("type", "vendor"), selectmode="browse")
            self.tree.heading("#0", text="Όνομα")
            self.tree.heading("type", text="Τύπος")
            self.tree.heading("vendor", text="Εταιρεία")
            self.tree.column("#0", width=220)
            self.tree.column("type", width=70, stretch=False)
            self.tree.column("vendor", width=110)
            sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.tree.bind("<<TreeviewSelect>>", self.on_select)
            btns = ttk.Frame(left)
            btns.pack(fill="x", pady=6)
            ttk.Button(btns, text="Νέο", command=self.new_profile).pack(side="left")
            ttk.Button(btns, text="Αντίγραφο", command=self.duplicate_profile).pack(side="left", padx=4)
            ttk.Button(btns, text="Διαγραφή", command=self.delete_profile).pack(side="left")

            # --- δεξιά: φόρμα ---
            head = ttk.Frame(right)
            head.pack(fill="x")
            head.columnconfigure(1, weight=1)
            ttk.Label(head, text="Όνομα προφίλ").grid(row=0, column=0, sticky="w", pady=3)
            self.name_var = tk.StringVar()
            self.name_entry = ttk.Entry(head, textvariable=self.name_var, font=("TkDefaultFont", 11))
            self.name_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
            ttk.Label(head, text="Βασικό προφίλ").grid(row=1, column=0, sticky="w", pady=3)
            self.inherits_var = tk.StringVar()
            self.inherits_cb = ttk.Combobox(head, textvariable=self.inherits_var)
            self.inherits_cb.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
            self.inherits_cb.bind("<KeyRelease>", self.filter_inherits)
            ttk.Label(head, text="Ό,τι αφήσεις κενό στη φόρμα, το παίρνει από το βασικό προφίλ. "
                                 "Χωρίς βασικό προφίλ = ανεξάρτητο preset.", style="Hint.TLabel",
                      wraplength=520, justify="left").grid(row=2, column=1, sticky="w", padx=8)

            nb = ttk.Notebook(right)
            nb.pack(fill="both", expand=True, pady=(10, 0))
            for title, fields, hint in SECTIONS:
                tab = ttk.Frame(nb, padding=14)
                nb.add(tab, text=title)
                self._build_section(tab, fields, hint)

            foot = ttk.Frame(right, padding=(0, 8))
            foot.pack(fill="x")
            ttk.Button(foot, text="Αποθήκευση", command=self.save_current).pack(side="left")
            ttk.Button(foot, text="Αναίρεση αλλαγών", command=self.revert_changes).pack(side="left", padx=6)
            self.updated_lbl = ttk.Label(foot, style="Hint.TLabel")
            self.updated_lbl.pack(side="right")

            status = ttk.Frame(self, padding=(10, 4))
            status.pack(fill="x")
            self.status_lbl = ttk.Label(status, style="Hint.TLabel")
            self.status_lbl.pack(side="left")
            self.update_status()

        def _build_section(self, tab, fields, hint):
            tab.columnconfigure(1, weight=1)
            row = 0
            for f in fields:
                if f.kind == "notes":
                    self.notes_text = tk.Text(tab, height=14, wrap="word", relief="solid", borderwidth=1)
                    self.notes_text.grid(row=row, column=0, columnspan=3, sticky="nsew")
                    tab.rowconfigure(row, weight=1)
                    row += 1
                    continue
                ttk.Label(tab, text=f.label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
                var = tk.StringVar()
                self.vars[f.key] = var
                cell = ttk.Frame(tab)
                cell.grid(row=row, column=1, sticky="w")
                if f.kind == "choice":
                    ttk.Combobox(cell, textvariable=var, values=f.options, width=14).pack(side="left")
                    if f.key == "filament_type":
                        var.trace_add("write", lambda *_: self.suggest_parent())
                elif f.kind == "bool":
                    ttk.Combobox(cell, textvariable=var, values=list(BOOL_LABELS.values()),
                                 state="readonly", width=18).pack(side="left")
                elif f.kind == "color":
                    ttk.Entry(cell, textvariable=var, width=10).pack(side="left")
                    sw = tk.Label(cell, width=4, relief="solid", borderwidth=1, cursor="hand2")
                    sw.pack(side="left", padx=6)
                    sw.bind("<Button-1>", lambda e, v=var: self.pick_color(v))
                    ttk.Button(cell, text="Επιλογή…", command=lambda v=var: self.pick_color(v)).pack(side="left")
                    var.trace_add("write", lambda *_, v=var, s=sw: self._update_swatch(v, s))
                else:
                    ttk.Entry(cell, textvariable=var, width=30 if f.kind == "text" else 12).pack(side="left")
                if f.unit:
                    ttk.Label(cell, text=f.unit, style="Hint.TLabel").pack(side="left", padx=6)
                row += 1
            if hint:
                ttk.Label(tab, text=hint, style="Hint.TLabel", wraplength=560).grid(
                    row=row, column=0, columnspan=3, sticky="w", pady=(12, 0))

        # ---------------- λίστα ----------------
        def swatch(self, color: str):
            color = color if re.fullmatch(r"#[0-9A-Fa-f]{6}", color or "") else ""
            if color not in self._swatches:
                img = tk.PhotoImage(width=14, height=14)
                img.put("#8a8a8a", to=(0, 0, 14, 14))
                img.put(color or "#ffffff", to=(1, 1, 13, 13))
                self._swatches[color] = img
            return self._swatches[color]

        def reload_library(self, select: str | None = None):
            self.profiles = {p["name"]: p for p in self.library.list_profiles()}
            self.render_list(select or self.current_name)
            self.update_status()

        def render_list(self, select: str | None = None):
            q = self.search_var.get().strip().lower()
            self._suppress_select = True
            self.tree.delete(*self.tree.get_children())
            self.iid_to_name.clear()
            for name, p in sorted(self.profiles.items(), key=lambda kv: kv[0].lower()):
                s = p["settings"]
                hay = " ".join([name, s.get("filament_type", ""), s.get("filament_vendor", "")]).lower()
                if q and q not in hay:
                    continue
                iid = self.tree.insert("", "end", text=name, image=self.swatch(s.get("filament_colour", "")),
                                       values=(s.get("filament_type", ""), s.get("filament_vendor", "")))
                self.iid_to_name[iid] = name
            target = select or self.current_name
            for iid, name in self.iid_to_name.items():
                if name == target:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
            self.after_idle(lambda: setattr(self, "_suppress_select", False))

        def select_first_or_new(self):
            first = self.tree.get_children()
            if first:
                name = self.iid_to_name[first[0]]
                self.load_into_form(self.profiles[name], name)
                self.render_list(name)
            else:
                self.new_profile()

        def on_select(self, _e=None):
            if self._suppress_select:
                return
            sel = self.tree.selection()
            if not sel:
                return
            name = self.iid_to_name.get(sel[0])
            if name is None or name == self.current_name:
                return
            if not self.maybe_save():
                self.render_list(self.current_name)
                return
            self.load_into_form(self.profiles[name], name)

        # ---------------- φόρμα ----------------
        def load_into_form(self, p: dict, stored_name: str | None):
            self._loading = True
            self.current_base = copy.deepcopy(p)
            self.current_name = stored_name
            self.name_var.set(p["name"])
            self.inherits_var.set(p["inherits"])
            for f in ALL_FIELDS:
                val = p["settings"].get(f.key, "")
                if f.kind == "notes":
                    self.notes_text.delete("1.0", "end")
                    self.notes_text.insert("1.0", val)
                elif f.kind == "bool":
                    self.vars[f.key].set(BOOL_LABELS.get(val, BOOL_LABELS[""]))
                else:
                    self.vars[f.key].set(val)
            self._loading = False
            self.snapshot = self._core(self.form_to_profile()) if stored_name else None
            if p.get("updated"):
                self.updated_lbl.config(text=f"Τελευταία αλλαγή: {p['updated'].replace('T', ' ')} "
                                             f"από {p.get('updated_by') or '—'}")
            else:
                self.updated_lbl.config(text="Δεν έχει αποθηκευτεί ακόμα")

        def form_to_profile(self) -> dict:
            p = copy.deepcopy(self.current_base)
            p["name"] = self.name_var.get().strip()
            p["inherits"] = self.inherits_var.get().strip()
            settings = {}
            for f in ALL_FIELDS:
                if f.kind == "notes":
                    raw = self.notes_text.get("1.0", "end-1c").rstrip()
                elif f.kind == "bool":
                    raw = BOOL_VALUES.get(self.vars[f.key].get(), "")
                else:
                    raw = self.vars[f.key].get()
                val = raw if f.kind == "notes" else normalize_value(f, raw)
                if val:
                    settings[f.key] = val
            p["settings"] = settings
            return p

        @staticmethod
        def _core(p):
            return (p["name"], p["inherits"], tuple(sorted(p["settings"].items())))

        def is_dirty(self) -> bool:
            return self.snapshot is None or self._core(self.form_to_profile()) != self.snapshot

        def maybe_save(self) -> bool:
            if not self.is_dirty():
                return True
            label = self.current_name or self.name_var.get() or "νέο προφίλ"
            ans = messagebox.askyesnocancel("Μη αποθηκευμένες αλλαγές",
                                            f"Να αποθηκευτούν οι αλλαγές στο «{label}»;", parent=self)
            if ans is None:
                return False
            return self.save_current() if ans else True

        def save_current(self) -> bool:
            p = self.form_to_profile()
            errors, warnings = validate_profile(p, self.valid_parents)
            if errors:
                messagebox.showerror("Δεν αποθηκεύτηκε", "\n".join(errors), parent=self)
                return False
            if p["name"] != self.current_name and self.library.exists(p["name"]):
                if not messagebox.askyesno("Υπάρχει ήδη",
                                           f"Υπάρχει ήδη προφίλ «{p['name']}». Να αντικατασταθεί;", parent=self):
                    return False
            if warnings and not messagebox.askyesno("Προσοχή", "\n\n".join(warnings) + "\n\nΑποθήκευση;",
                                                    parent=self):
                return False
            try:
                self.library.save(p, old_name=self.current_name)
            except OSError as e:
                messagebox.showerror("Σφάλμα αποθήκευσης",
                                     f"Το αρχείο δεν γράφτηκε στη βιβλιοθήκη:\n{e}", parent=self)
                return False
            self.load_into_form(p, p["name"])
            self.reload_library(p["name"])
            self.set_status(f"Αποθηκεύτηκε: {p['name']}")
            return True

        def revert_changes(self):
            if self.current_name and self.library.exists(self.current_name):
                self.load_into_form(self.library.load(self.current_name), self.current_name)

        def unique_name(self, base: str) -> str:
            name, i = base, 2
            while name in self.profiles:
                name, i = f"{base} {i}", i + 1
            return name

        def new_profile(self):
            if not self.maybe_save():
                return
            parent = "Generic PLA @System" if "Generic PLA @System" in self.valid_parents else ""
            p = new_profile(self.unique_name("Νέο filament"), parent)
            p["settings"] = {"filament_type": "PLA", "filament_diameter": "1.75"}
            self.load_into_form(p, None)
            self._suppress_select = True
            self.tree.selection_remove(self.tree.selection())
            self.after_idle(lambda: setattr(self, "_suppress_select", False))
            self.name_entry.focus_set()
            self.name_entry.select_range(0, "end")

        def duplicate_profile(self):
            p = self.form_to_profile()
            if not self.maybe_save():
                return
            p["name"] = self.unique_name(f"{p['name']} (αντίγραφο)")
            p["updated"] = ""
            self.load_into_form(p, None)
            self.name_entry.focus_set()
            self.name_entry.select_range(0, "end")

        def delete_profile(self):
            name = self.current_name
            if not name:   # νέο, μη αποθηκευμένο: απλώς απορρίπτεται
                self.snapshot = self._core(self.form_to_profile())
                self.select_first_or_new()
                return
            if not messagebox.askyesno("Διαγραφή", f"Να διαγραφεί το «{name}» από τη βιβλιοθήκη;\n"
                                       "(Κρατιέται αντίγραφο στον φάκελο .history)", parent=self):
                return
            self.library.delete(name)
            fdir = self.orca_fdir()
            orca_file = fdir / f"{name}.json" if fdir else None
            if orca_file and orca_file.exists() and messagebox.askyesno(
                    "Διαγραφή από Orca", f"Να αφαιρεθεί το «{name}» και από το OrcaSlicer;", parent=self):
                orca_file.unlink(missing_ok=True)
                orca_file.with_suffix(".info").unlink(missing_ok=True)
            self.current_name = None
            self.snapshot = self._core(self.form_to_profile())
            self.reload_library()
            self.select_first_or_new()

        # ---------------- βοηθητικά φόρμας ----------------
        def pick_color(self, var):
            cur = var.get() if re.fullmatch(r"#[0-9A-Fa-f]{6}", var.get() or "") else "#FFFFFF"
            _, hexval = colorchooser.askcolor(color=cur, parent=self, title="Χρώμα filament")
            if hexval:
                var.set(hexval.upper())

        @staticmethod
        def _update_swatch(var, label):
            v = var.get().strip()
            if re.fullmatch(r"#?[0-9A-Fa-f]{6}", v):
                label.config(bg=v if v.startswith("#") else "#" + v)
            else:
                label.config(bg=label.master.winfo_toplevel().cget("bg"))

        def filter_inherits(self, event=None):
            if event and event.keysym in ("Down", "Up", "Return", "Escape"):
                return
            typed = self.inherits_var.get().strip().lower()
            vals = [n for n in self.system_names if typed in n.lower()] if typed else self.system_names
            self.inherits_cb["values"] = vals[:800]

        def suggest_parent(self):
            """Όταν αλλάζει ο τύπος, προτείνει το αντίστοιχο Generic βασικό προφίλ του Orca."""
            if self._loading:
                return
            cur = self.inherits_var.get().strip()
            if not (cur.startswith("Generic ") and cur.endswith("@System")):
                return
            candidate = f"Generic {self.vars['filament_type'].get().strip()} @System"
            if candidate in self.valid_parents:
                self.inherits_var.set(candidate)

        # ---------------- system presets (στο παρασκήνιο) ----------------
        def start_system_scan(self):
            orca_dir = self.cfg["orca_dir"]
            if not orca_dir:
                return
            self.set_status("Φόρτωση βασικών προφίλ από το OrcaSlicer…")
            threading.Thread(target=lambda: self._scan_queue.put(scan_system_filaments(orca_dir)),
                             daemon=True).start()
            self.after(150, self._poll_scan)

        def _poll_scan(self):
            try:
                names, valid = self._scan_queue.get_nowait()
            except queue.Empty:
                self.after(150, self._poll_scan)
                return
            self.system_names, self.valid_parents = names, valid
            self.inherits_cb["values"] = names[:800]
            self.update_status()

        # ---------------- Orca ----------------
        def orca_fdir(self) -> Path | None:
            return orca_filament_dir(self.cfg["orca_dir"], self.cfg["orca_user"]) if self.cfg["orca_dir"] else None

        def export_selected(self):
            if self.is_dirty() and not self.save_current():
                return
            if self.current_name:
                self.export_profiles([self.library.load(self.current_name)])

        def export_all(self):
            if not self.maybe_save():
                return
            profiles = self.library.list_profiles()
            if not profiles:
                messagebox.showinfo("Κενή βιβλιοθήκη", "Δεν υπάρχουν προφίλ για αποστολή.", parent=self)
                return
            if messagebox.askyesno("Αποστολή όλων",
                                   f"Να σταλούν και τα {len(profiles)} προφίλ στο OrcaSlicer;", parent=self):
                self.export_profiles(profiles)

        def export_profiles(self, profiles: list):
            fdir = self.orca_fdir()
            if not fdir or not Path(self.cfg["orca_dir"]).is_dir():
                messagebox.showerror("Δεν βρέθηκε το OrcaSlicer",
                                     "Όρισε τον φάκελο δεδομένων του OrcaSlicer στις Ρυθμίσεις.", parent=self)
                return
            system = set(self.system_names)
            problems, ok = [], []
            for p in profiles:
                errors, _ = validate_profile(p)
                if p["name"] in system:
                    errors.append("έχει το ίδιο όνομα με system preset του Orca")
                if p["inherits"] and self.valid_parents and p["inherits"] not in self.valid_parents:
                    errors.append(f"άγνωστο βασικό προφίλ «{p['inherits']}»")
                (problems.append(f"• {p['name']}: {'; '.join(errors)}") if errors else ok.append(p))
            existing = [p["name"] for p in ok if (fdir / f"{p['name']}.json").exists()]
            msg = []
            if existing:
                shown = ", ".join(existing[:8]) + (" …" if len(existing) > 8 else "")
                msg.append(f"Θα αντικατασταθούν {len(existing)} υπάρχοντα presets στο Orca: {shown}")
            if problems:
                msg.append("Θα παραλειφθούν:\n" + "\n".join(problems[:10]))
            if not ok:
                messagebox.showerror("Τίποτα για αποστολή", "\n\n".join(msg), parent=self)
                return
            if msg and not messagebox.askyesno("Επιβεβαίωση", "\n\n".join(msg) + "\n\nΣυνέχεια;", parent=self):
                return
            version = detect_orca_version(self.cfg["orca_dir"])
            try:
                for p in ok:
                    export_to_orca(p, fdir, version)
            except OSError as e:
                messagebox.showerror("Σφάλμα", f"Αποτυχία εγγραφής στο OrcaSlicer:\n{e}", parent=self)
                return
            text = f"Στάλθηκαν {len(ok)} προφίλ στο OrcaSlicer."
            if is_orca_running():
                text += ("\n\nΤο OrcaSlicer είναι ανοιχτό. Κλείσ' το και άνοιξέ το ξανά "
                         "για να εμφανιστούν οι αλλαγές.")
                messagebox.showinfo("Έτοιμο", text, parent=self)
            elif messagebox.askyesno("Έτοιμο", text + "\n\nΝα ανοίξει τώρα το OrcaSlicer;", parent=self):
                self.on_launch()
            self.set_status(text.split("\n")[0])

        def on_launch(self):
            if is_orca_running():
                messagebox.showinfo("OrcaSlicer", "Το OrcaSlicer είναι ήδη ανοιχτό.", parent=self)
                return
            err = launch_orca(self.cfg["orca_exe"])
            if err:
                messagebox.showerror("OrcaSlicer", err, parent=self)

        def import_from_orca(self):
            fdir = self.orca_fdir()
            presets = read_orca_user_presets(fdir) if fdir else []
            if not presets:
                messagebox.showinfo("Εισαγωγή", f"Δεν βρέθηκαν user filament presets στο:\n{fdir}", parent=self)
                return
            if not self.maybe_save():
                return
            win = tk.Toplevel(self)
            win.title("Εισαγωγή από OrcaSlicer")
            win.geometry("520x480")
            win.transient(self)
            ttk.Label(win, text="Διάλεξε ποια presets θα μπουν στη βιβλιοθήκη "
                                "(Ctrl/Shift για πολλαπλή επιλογή).", padding=10, wraplength=480).pack(anchor="w")
            frame = ttk.Frame(win, padding=(10, 0))
            frame.pack(fill="both", expand=True)
            lb = tk.Listbox(frame, selectmode="extended", activestyle="none")
            sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            lb.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            for p in presets:
                lb.insert("end", p["name"] + ("   (υπάρχει ήδη)" if p["name"] in self.profiles else ""))

            def do_import():
                chosen = [presets[i] for i in lb.curselection()]
                if not chosen:
                    return
                clash = [p["name"] for p in chosen if p["name"] in self.profiles]
                if clash and not messagebox.askyesno(
                        "Αντικατάσταση", f"{len(clash)} προφίλ υπάρχουν ήδη στη βιβλιοθήκη και "
                                         "θα αντικατασταθούν. Συνέχεια;", parent=win):
                    return
                for p in chosen:
                    self.library.save(p)
                win.destroy()
                self.current_name = None
                self.reload_library(chosen[0]["name"])
                self.load_into_form(self.library.load(chosen[0]["name"]), chosen[0]["name"])
                self.set_status(f"Εισήχθησαν {len(chosen)} προφίλ από το OrcaSlicer")

            b = ttk.Frame(win, padding=10)
            b.pack(fill="x")
            ttk.Button(b, text="Επιλογή όλων", command=lambda: lb.select_set(0, "end")).pack(side="left")
            ttk.Button(b, text="Άκυρο", command=win.destroy).pack(side="right")
            ttk.Button(b, text="Εισαγωγή", command=do_import).pack(side="right", padx=6)
            win.grab_set()

        # ---------------- ρυθμίσεις ----------------
        def open_settings(self):
            win = tk.Toplevel(self)
            win.title("Ρυθμίσεις")
            win.transient(self)
            win.resizable(True, False)
            body = ttk.Frame(win, padding=14)
            body.pack(fill="both", expand=True)
            body.columnconfigure(1, weight=1)
            v_lib = tk.StringVar(value=self.cfg["library_dir"])
            v_orca = tk.StringVar(value=self.cfg["orca_dir"])
            v_user = tk.StringVar(value=self.cfg["orca_user"])
            v_exe = tk.StringVar(value=self.cfg["orca_exe"])

            def browse_dir(var):
                d = filedialog.askdirectory(parent=win, initialdir=var.get() or str(Path.home()))
                if d:
                    var.set(d)

            def browse_exe():
                f = filedialog.askopenfilename(parent=win, title="Πρόγραμμα OrcaSlicer")
                if f:
                    v_exe.set(f)

            rows = [
                ("Φάκελος βιβλιοθήκης", v_lib, lambda: browse_dir(v_lib),
                 "Μπορεί να είναι κοινόχρηστος φάκελος στον server, π.χ. \\\\SERVER\\3D\\FilamentLibrary"),
                ("Φάκελος δεδομένων OrcaSlicer", v_orca, lambda: browse_dir(v_orca),
                 "Ο φάκελος που περιέχει τους υποφακέλους user και system."),
            ]
            r = 0
            for label, var, cmd, hint in rows:
                ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=(6, 0))
                ttk.Entry(body, textvariable=var, width=60).grid(row=r, column=1, sticky="ew", padx=8, pady=(6, 0))
                ttk.Button(body, text="Αναζήτηση…", command=cmd).grid(row=r, column=2, pady=(6, 0))
                ttk.Label(body, text=hint, style="Hint.TLabel").grid(row=r + 1, column=1, sticky="w", padx=8)
                r += 2
            ttk.Label(body, text="Χρήστης OrcaSlicer").grid(row=r, column=0, sticky="w", pady=(6, 0))
            user_cb = ttk.Combobox(body, textvariable=v_user, width=30)
            user_cb.grid(row=r, column=1, sticky="w", padx=8, pady=(6, 0))
            ttk.Label(body, text="«default» όταν δεν είσαι συνδεδεμένος σε λογαριασμό στο Orca.",
                      style="Hint.TLabel").grid(row=r + 1, column=1, sticky="w", padx=8)
            r += 2
            ttk.Label(body, text="Πρόγραμμα OrcaSlicer").grid(row=r, column=0, sticky="w", pady=(6, 0))
            ttk.Entry(body, textvariable=v_exe, width=60).grid(row=r, column=1, sticky="ew", padx=8, pady=(6, 0))
            ttk.Button(body, text="Αναζήτηση…", command=browse_exe).grid(row=r, column=2, pady=(6, 0))
            r += 1

            def refresh_users(*_):
                user_cb["values"] = list_orca_users(v_orca.get()) if v_orca.get() else ["default"]
            v_orca.trace_add("write", refresh_users)
            refresh_users()

            def autodetect():
                d = detect_orca_dir()
                if d:
                    v_orca.set(d)
                    v_user.set(detect_orca_user(d))
                e = detect_orca_exe()
                if e:
                    v_exe.set(e)
                if not d:
                    messagebox.showinfo("Αυτόματος εντοπισμός", "Δεν βρέθηκε εγκατάσταση του OrcaSlicer. "
                                        "Άνοιξε το Orca μία φορά ή όρισε τον φάκελο χειροκίνητα.", parent=win)

            def ok():
                if not self.maybe_save():
                    return
                changed_orca = v_orca.get() != self.cfg["orca_dir"]
                self.cfg.update(library_dir=v_lib.get().strip(), orca_dir=v_orca.get().strip(),
                                orca_user=v_user.get().strip() or "default", orca_exe=v_exe.get().strip())
                save_config(self.cfg)
                self.library = Library(self.cfg["library_dir"])
                self.current_name = None
                self.snapshot = self._core(self.form_to_profile())
                self.reload_library()
                self.select_first_or_new()
                if changed_orca:
                    self.start_system_scan()
                win.destroy()

            b = ttk.Frame(win, padding=(14, 0, 14, 14))
            b.pack(fill="x")
            ttk.Button(b, text="Αυτόματος εντοπισμός Orca", command=autodetect).pack(side="left")
            ttk.Button(b, text="Άκυρο", command=win.destroy).pack(side="right")
            ttk.Button(b, text="Αποθήκευση", command=ok).pack(side="right", padx=6)
            win.grab_set()

        def open_library_folder(self):
            path = Path(self.cfg["library_dir"])
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

        # ---------------- κατάσταση ----------------
        def set_status(self, text: str):
            self.status_lbl.config(text=text)
            self.after(6000, self.update_status)

        def update_status(self):
            fdir = self.orca_fdir()
            parts = [f"Βιβλιοθήκη: {self.cfg['library_dir']}  ({len(self.profiles)} προφίλ)",
                     f"Orca: {fdir}" if fdir else "Orca: δεν έχει οριστεί"]
            if self.system_names:
                parts.append(f"{len(self.system_names)} βασικά προφίλ")
            self.status_lbl.config(text="     |     ".join(parts))

        def on_close(self):
            if self.maybe_save():
                self.destroy()

    App().mainloop()


def main():
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--sync", action="store_true", help="στέλνει όλη τη βιβλιοθήκη στο OrcaSlicer (χωρίς UI)")
    ap.add_argument("--launch", action="store_true", help="ανοίγει το OrcaSlicer μετά το --sync")
    args = ap.parse_args()
    if args.sync:
        sys.exit(cli_sync(args.launch))
    run_gui()


if __name__ == "__main__":
    main()
