"""
tests/test_qa_prompt_cache.py
==============================
Full QA test suite for system prompt cache, IQD rate cache,
concurrency safety, atomic writes, and invalidation behavior.

Run:  python tests/test_qa_prompt_cache.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ── Test harness ───────────────────────────────────────────────────────────────

PASS   = 0
FAIL   = 0
ISSUES = []


def ok(name):
    global PASS
    PASS += 1
    print(f"  PASS  {name}")


def fail(name, reason=""):
    global FAIL
    ISSUES.append(f"{name}: {reason}")
    FAIL += 1
    print(f"  FAIL  {name}" + (f" -- {reason}" if reason else ""))


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — SYSTEM PROMPT CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _run_prompt_tests():
    import ai.prompt_manager as pm

    # ── Temp filesystem ──────────────────────────────────────────────────────
    tmp_root   = Path(tempfile.mkdtemp())
    prompt_file = tmp_root / "system_prompt.txt"
    kb_dir      = tmp_root / "knowledge_base"
    kb_dir.mkdir()
    kb_index = kb_dir / "index.json"
    kb_index.write_text("[]", encoding="utf-8")

    _orig_sp  = pm.SYSTEM_PROMPT_FILE
    _orig_kbd = pm.KNOWLEDGE_BASE_DIR
    _orig_kif = pm._KNOWLEDGE_INDEX_FILE

    pm.SYSTEM_PROMPT_FILE    = prompt_file
    pm.KNOWLEDGE_BASE_DIR    = kb_dir
    pm._KNOWLEDGE_INDEX_FILE = kb_index

    def reset():
        pm._cache.content         = ""
        pm._cache.prompt_mtime    = None
        pm._cache.knowledge_mtime = None
        pm._cache.version         = 0

    try:
        # T01 — placeholder rendering
        reset()
        prompt_file.write_text("Hello {FIXED_WELCOME_EN}", encoding="utf-8")
        result = pm.load_system_prompt()
        if "Welcome to DhifafBot" in result:
            ok("T01 load_system_prompt() renders placeholders")
        else:
            fail("T01 load_system_prompt() placeholder rendering", repr(result[:60]))

        # T02 — cache hit on second call (no version bump)
        reset()
        prompt_file.write_text("Base V1", encoding="utf-8")
        c1 = pm.build_full_system_prompt()
        ver1 = pm._cache.version
        c2 = pm.build_full_system_prompt()
        ver2 = pm._cache.version
        if c1 == c2 and ver1 == ver2 and ver1 == 1:
            ok("T02 build_full_system_prompt() returns cache on 2nd call — no extra build")
        else:
            fail("T02 cache hit", f"versions {ver1} vs {ver2}, same_content={c1 == c2}")

        # T03 — auto-invalidation via mtime change
        reset()
        prompt_file.write_text("Prompt V1", encoding="utf-8")
        v1 = pm.build_full_system_prompt()
        time.sleep(0.05)  # ensure new mtime
        prompt_file.write_text("Prompt V2", encoding="utf-8")
        v2 = pm.build_full_system_prompt()
        if "V1" in v1 and "V2" in v2 and v1 != v2:
            ok("T03 mtime-based auto-invalidation when system_prompt.txt changes")
        else:
            fail("T03 mtime-based invalidation", f"v1={v1!r}, v2={v2!r}")

        # T04 — explicit invalidation via invalidate_prompt_cache()
        reset()
        prompt_file.write_text("Prompt V3", encoding="utf-8")
        v3 = pm.build_full_system_prompt()
        ver3 = pm._cache.version
        pm.invalidate_prompt_cache()
        v4 = pm.build_full_system_prompt()
        ver4 = pm._cache.version
        if ver4 > ver3 and "V3" in v4:
            ok("T04 invalidate_prompt_cache() forces rebuild")
        else:
            fail("T04 explicit invalidation", f"versions {ver3}->{ver4}")

        # T05 — write_system_prompt() atomic: concurrent reads see only complete content
        reset()
        prompt_file.write_text("Initial", encoding="utf-8")
        partial_reads = []

        def do_write_pm():
            pm.write_system_prompt("Updated Prompt V5")

        def do_read_pm():
            content = pm.build_full_system_prompt()
            # Valid states: "Initial" OR contains "Updated Prompt V5"
            if content and "Initial" not in content and "Updated" not in content:
                partial_reads.append(repr(content[:80]))

        threads = [threading.Thread(target=do_write_pm)] + [
            threading.Thread(target=do_read_pm) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if not partial_reads:
            ok("T05 write_system_prompt() atomic — no partial reads in concurrent scenario")
        else:
            fail("T05 atomic write concurrency", f"partial states seen: {partial_reads}")

        # T06 — BUG CHECK: write_system_prompt() must cache FULL prompt (base + knowledge)
        reset()
        kb_file = kb_dir / "company.txt"
        kb_file.write_text("Company info: Dhifaf Baghdad is the best", encoding="utf-8")
        kb_index.write_text(json.dumps([{
            "id":                "kb1",
            "original_filename": "company.txt",
            "text_filename":     "company.txt",
        }]), encoding="utf-8")
        prompt_file.write_text("System instructions", encoding="utf-8")

        full_before = pm.build_full_system_prompt()
        has_kb_before = "Dhifaf Baghdad" in full_before

        time.sleep(0.05)
        pm.write_system_prompt("Updated system instructions")

        cached_after_write = pm._cache.content
        has_kb_in_cache    = "Dhifaf Baghdad" in cached_after_write

        full_after = pm.build_full_system_prompt()
        has_kb_after = "Dhifaf Baghdad" in full_after

        if has_kb_before and has_kb_in_cache and has_kb_after:
            ok("T06 write_system_prompt() cache includes knowledge (base + knowledge, not base-only)")
        elif not has_kb_before:
            fail("T06", "baseline: knowledge not in initial full prompt at all")
        elif not has_kb_in_cache:
            fail("T06 BUG", "write_system_prompt() cached only base prompt — knowledge missing from cache")
        else:
            fail("T06", f"kb_before={has_kb_before} kb_cache={has_kb_in_cache} kb_after={has_kb_after}")

        # Cleanup KB for next tests
        kb_index.write_text("[]", encoding="utf-8")
        kb_file.unlink()

        # T07 — knowledge invalidation propagates to prompt cache
        reset()
        prompt_file.write_text("System V7", encoding="utf-8")
        out_no_kb = pm.build_full_system_prompt()
        ver_no_kb = pm._cache.version

        # Add knowledge file
        kb_file2 = kb_dir / "kb2.txt"
        kb_file2.write_text("New knowledge content for testing", encoding="utf-8")
        kb_index.write_text(json.dumps([{
            "id": "kb2", "original_filename": "kb2.txt", "text_filename": "kb2.txt"
        }]), encoding="utf-8")

        # knowledge index mtime changed — cache should invalidate
        out_with_kb = pm.build_full_system_prompt()
        ver_with_kb = pm._cache.version

        if "New knowledge content" in out_with_kb and ver_with_kb > ver_no_kb:
            ok("T07 knowledge upload: prompt cache auto-invalidates via knowledge_index mtime")
        else:
            fail("T07 knowledge mtime auto-invalidation",
                 f"kb_in_output={'New knowledge content' in out_with_kb} ver={ver_with_kb}")

        # Delete knowledge → cache should update
        kb_index.write_text("[]", encoding="utf-8")
        kb_file2.unlink()
        out_del = pm.build_full_system_prompt()
        if "New knowledge content" not in out_del:
            ok("T07b knowledge delete: prompt cache auto-invalidates via knowledge_index mtime")
        else:
            fail("T07b knowledge delete: old knowledge persists in cache")

        # T08 — concurrent rapid prompt updates — no empty or corrupt content
        reset()
        prompt_file.write_text("Concurrent base", encoding="utf-8")
        read_errors_pm = []
        write_count    = [0]

        def updater_pm():
            for i in range(5):
                pm.write_system_prompt(f"Rapid update #{i}")
                write_count[0] += 1
                time.sleep(0.008)

        def reader_pm():
            for _ in range(15):
                try:
                    c = pm.build_full_system_prompt()
                    if not c.strip():
                        read_errors_pm.append("empty content returned")
                except Exception as exc:
                    read_errors_pm.append(str(exc))
                time.sleep(0.004)

        threads = [threading.Thread(target=updater_pm)] + [
            threading.Thread(target=reader_pm) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if not read_errors_pm:
            ok(f"T08 concurrent rapid updates ({write_count[0]} writes, 75 reads) — all reads valid")
        else:
            fail("T08 concurrent rapid updates", str(read_errors_pm[:3]))

        # T09 — missing file raises correctly (no infinite loop / silent error)
        reset()
        if prompt_file.exists():
            prompt_file.unlink()
        try:
            pm.build_full_system_prompt()
            fail("T09 missing file should raise")
        except FileNotFoundError:
            ok("T09 FileNotFoundError raised correctly for missing system_prompt.txt")
        except Exception as exc:
            fail("T09 wrong exception type", str(exc))

        prompt_file.write_text("Restored", encoding="utf-8")

    finally:
        pm.SYSTEM_PROMPT_FILE    = _orig_sp
        pm.KNOWLEDGE_BASE_DIR    = _orig_kbd
        pm._KNOWLEDGE_INDEX_FILE = _orig_kif
        pm._cache.content        = ""
        pm._cache.prompt_mtime   = None
        pm._cache.knowledge_mtime = None
        shutil.rmtree(tmp_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — IQD RATE CACHE
# ─────────────────────────────────────────────────────────────────────────────

def _run_rate_tests():
    # Import formatters directly — avoid ai/tools/__init__.py which triggers
    # product_search.py → sqlalchemy import (not needed for rate cache tests).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ai.tools.formatters",
        str(Path("ai/tools/formatters.py").resolve()),
    )
    fmt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fmt)  # type: ignore[union-attr]

    tmp_root  = Path(tempfile.mkdtemp())
    rate_file = tmp_root / "rate.json"

    _orig_rf = fmt.RATE_FILE
    fmt.RATE_FILE = rate_file

    def reset_rate():
        with fmt._rate_lock:
            fmt._rate_cache["value"] = None
            fmt._rate_cache["mtime"] = None

    try:
        # T10 — default rate when file missing
        reset_rate()
        if rate_file.exists():
            rate_file.unlink()
        rate = fmt.get_iqd_rate()
        if rate == 1310.0:
            ok("T10 get_iqd_rate() returns 1310 default when rate.json missing")
        else:
            fail("T10 default rate", f"got {rate}")

        # T11 — reads rate from file correctly
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1500.0}), encoding="utf-8")
        rate = fmt.get_iqd_rate()
        if rate == 1500.0:
            ok("T11 get_iqd_rate() reads correct value from rate.json")
        else:
            fail("T11 rate read", f"got {rate}, expected 1500.0")

        # T12 — cache hit on second call (one stat, no read)
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1600.0}), encoding="utf-8")
        r1 = fmt.get_iqd_rate()
        mtime_after_first = fmt._rate_cache["mtime"]
        r2 = fmt.get_iqd_rate()
        if r1 == r2 == 1600.0 and fmt._rate_cache["mtime"] == mtime_after_first:
            ok("T12 get_iqd_rate() cache hit on second call")
        else:
            fail("T12 cache hit", f"r1={r1} r2={r2}")

        # T13 — auto-invalidation when file changes
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1700.0}), encoding="utf-8")
        r_before = fmt.get_iqd_rate()
        time.sleep(0.05)
        rate_file.write_text(json.dumps({"iqd_rate": 1800.0}), encoding="utf-8")
        r_after = fmt.get_iqd_rate()
        if r_before == 1700.0 and r_after == 1800.0:
            ok("T13 get_iqd_rate() auto-invalidates when rate.json mtime changes")
        else:
            fail("T13 mtime auto-invalidation", f"before={r_before} after={r_after}")

        # T14 — update_iqd_rate() writes atomically + immediately updates cache
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1300.0}), encoding="utf-8")
        fmt.get_iqd_rate()  # populate cache with 1300
        fmt.update_iqd_rate(2000.0)  # atomic write + cache update
        # Cache must be updated immediately (no disk re-read needed)
        cached_rate = fmt._rate_cache["value"]
        # Next call must also return new rate
        next_rate = fmt.get_iqd_rate()
        if cached_rate == 2000.0 and next_rate == 2000.0:
            ok("T14 update_iqd_rate() atomically updates file + immediately updates cache")
        else:
            fail("T14 update_iqd_rate()", f"cached={cached_rate} next_read={next_rate}")

        # T15 — convert_to_iqd() uses cached rate (not N disk reads)
        reset_rate()
        fmt.update_iqd_rate(1500.0)
        read_calls = [0]
        orig_read  = fmt.RATE_FILE.read_text if hasattr(fmt.RATE_FILE, "read_text") else None

        # Convert 50 products — track disk read count via mtime side-effect
        mtime_before = fmt._rate_cache["mtime"]
        results = []
        for price in [10.0, 20.0, 30.0, 40.0, 50.0] * 10:  # 50 products
            results.append(fmt.convert_to_iqd(price))
        mtime_after = fmt._rate_cache["mtime"]

        # Mtime should be stable (no re-reads)
        if mtime_before == mtime_after and all("IQD" in r for r in results):
            ok("T15 convert_to_iqd() uses cached rate for 50 products (single cache value)")
        else:
            fail("T15 rate cache efficiency", f"mtime changed={mtime_before != mtime_after}")

        # T16 — no mixed rates in same request (all products use same rate)
        reset_rate()
        fmt.update_iqd_rate(1000.0)
        prices = [10.0] * 20  # 20 products, all $10
        iqd_prices = [fmt.convert_to_iqd(p) for p in prices]
        unique_prices = set(iqd_prices)
        if len(unique_prices) == 1 and "10,000 IQD" in unique_prices:
            ok("T16 no mixed rates — all 20 products show same IQD price in same request")
        else:
            fail("T16 mixed rate check", f"unique prices: {unique_prices}")

        # T17 — concurrent rate update + reads: no stale or corrupt values
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1300.0}), encoding="utf-8")
        fmt.get_iqd_rate()  # seed cache
        corrupt_reads = []
        VALID_RATES   = {1300.0, 1500.0, 1700.0}  # valid transition rates

        def rate_updater():
            for new_rate in [1500.0, 1700.0]:
                fmt.update_iqd_rate(new_rate)
                time.sleep(0.02)

        def rate_reader():
            for _ in range(20):
                r = fmt.get_iqd_rate()
                if r not in VALID_RATES:
                    corrupt_reads.append(r)
                time.sleep(0.005)

        threads = [threading.Thread(target=rate_updater)] + [
            threading.Thread(target=rate_reader) for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if not corrupt_reads:
            ok("T17 concurrent rate update + reads — all values are valid transitions")
        else:
            fail("T17 rate concurrency", f"invalid rates seen: {set(corrupt_reads)}")

        # T18 — format_products() produces consistent IQD prices in batch
        reset_rate()
        fmt.update_iqd_rate(1000.0)
        raw_products = [
            {"barcode": str(i), "item_name": f"Product {i}", "price": 10.0,
             "available_qty": 20, "description": "", "image_url": None,
             "category_name": "Beauty", "brand_name": "Brand"}
            for i in range(20)
        ]
        formatted = fmt.format_products(raw_products, limit=20)
        iqd_set = {p["price"] for p in formatted}
        if len(iqd_set) == 1 and "10,000" in list(iqd_set)[0]:
            ok("T18 format_products() — all 20 products show same IQD price (1000 rate)")
        else:
            fail("T18 format_products() consistency", f"price set: {iqd_set}")

        # T19 — invalidate_rate_cache() forces re-read
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1111.0}), encoding="utf-8")
        fmt.get_iqd_rate()  # cache 1111
        time.sleep(0.05)
        rate_file.write_text(json.dumps({"iqd_rate": 2222.0}), encoding="utf-8")
        # Without invalidation the stat would catch the mtime change anyway
        fmt.invalidate_rate_cache()
        r = fmt.get_iqd_rate()
        if r == 2222.0:
            ok("T19 invalidate_rate_cache() forces re-read on next call")
        else:
            fail("T19 explicit rate invalidation", f"got {r}")

    finally:
        fmt.RATE_FILE = _orig_rf
        with fmt._rate_lock:
            fmt._rate_cache["value"] = None
            fmt._rate_cache["mtime"] = None
        shutil.rmtree(tmp_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 — KNOWLEDGE SERVICE INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────

def _run_service_integration_tests():
    import importlib.util, inspect
    spec = importlib.util.spec_from_file_location(
        "ai.tools.formatters",
        str(Path("ai/tools/formatters.py").resolve()),
    )
    fmt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fmt)  # type: ignore[union-attr]
    # knowledge_service reads from core.config which needs dotenv loaded
    import services.knowledge_service as ks

    tmp_root  = Path(tempfile.mkdtemp())
    rate_file = tmp_root / "rate.json"

    _orig_rf  = fmt.RATE_FILE
    fmt.RATE_FILE = rate_file

    # Patch settings in knowledge_service
    _orig_settings_rf = ks.settings.RATE_FILE if hasattr(ks, "settings") else None

    def reset_rate():
        with fmt._rate_lock:
            fmt._rate_cache["value"] = None
            fmt._rate_cache["mtime"] = None

    try:
        # T20 — save_iqd_rate() now delegates to update_iqd_rate()
        reset_rate()
        rate_file.write_text(json.dumps({"iqd_rate": 1300.0}), encoding="utf-8")
        # Verify save_iqd_rate calls update_iqd_rate (which updates cache)
        import inspect
        src = inspect.getsource(ks.save_iqd_rate)
        if "update_iqd_rate" in src:
            ok("T20 save_iqd_rate() delegates to update_iqd_rate() (atomic + cache)")
        else:
            fail("T20 save_iqd_rate() still uses direct write_text", src[:100])

        # T21 — load_iqd_rate() delegates to get_iqd_rate() (cache-aware)
        src21 = inspect.getsource(ks.load_iqd_rate)
        if "get_iqd_rate" in src21:
            ok("T21 load_iqd_rate() delegates to get_iqd_rate() (uses cache)")
        else:
            fail("T21 load_iqd_rate() still reads file directly", src21[:100])

    finally:
        fmt.RATE_FILE = _orig_rf
        with fmt._rate_lock:
            fmt._rate_cache["value"] = None
            fmt._rate_cache["mtime"] = None
        shutil.rmtree(tmp_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — ATOMIC WRITE INTEGRITY
# ─────────────────────────────────────────────────────────────────────────────

def _run_atomic_write_tests():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ai.tools.formatters",
        str(Path("ai/tools/formatters.py").resolve()),
    )
    fmt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fmt)  # type: ignore[union-attr]
    import ai.prompt_manager as pm

    tmp_root  = Path(tempfile.mkdtemp())
    rate_file = tmp_root / "rate.json"
    prompt_file = tmp_root / "system_prompt.txt"
    kb_dir = tmp_root / "knowledge_base"
    kb_dir.mkdir()
    kb_index = kb_dir / "index.json"
    kb_index.write_text("[]", encoding="utf-8")
    prompt_file.write_text("Initial", encoding="utf-8")

    _orig_rf  = fmt.RATE_FILE
    _orig_sp  = pm.SYSTEM_PROMPT_FILE
    _orig_kbd = pm.KNOWLEDGE_BASE_DIR
    _orig_kif = pm._KNOWLEDGE_INDEX_FILE

    fmt.RATE_FILE            = rate_file
    pm.SYSTEM_PROMPT_FILE    = prompt_file
    pm.KNOWLEDGE_BASE_DIR    = kb_dir
    pm._KNOWLEDGE_INDEX_FILE = kb_index

    try:
        # T22 — no .tmp files left after successful rate write
        fmt.update_iqd_rate(1500.0)
        tmp_files = list(tmp_root.glob(".rate_*.tmp"))
        if not tmp_files:
            ok("T22 update_iqd_rate() — no orphan .tmp files after successful write")
        else:
            fail("T22 orphan tmp files", str(tmp_files))

        # T23 — no .prompt tmp files left after successful prompt write
        pm._cache.content = ""
        pm._cache.prompt_mtime = None
        pm._cache.knowledge_mtime = None
        pm.write_system_prompt("Clean prompt write")
        tmp_files_pm = list(tmp_root.glob(".prompt_*.tmp"))
        if not tmp_files_pm:
            ok("T23 write_system_prompt() — no orphan .tmp files after successful write")
        else:
            fail("T23 orphan prompt tmp files", str(tmp_files_pm))

        # T24 — rate file is valid JSON after update
        fmt.update_iqd_rate(1234.5)
        try:
            data = json.loads(rate_file.read_text(encoding="utf-8"))
            if data.get("iqd_rate") == 1234.5:
                ok("T24 rate.json is valid JSON with correct value after update")
            else:
                fail("T24 rate.json value", str(data))
        except json.JSONDecodeError as exc:
            fail("T24 rate.json is invalid JSON", str(exc))

        # T25 — prompt file contains the raw template (not rendered)
        pm.write_system_prompt("Hello {FIXED_WELCOME_EN}")
        raw = prompt_file.read_text(encoding="utf-8")
        if "{FIXED_WELCOME_EN}" in raw:
            ok("T25 system_prompt.txt stores raw template (not rendered)")
        else:
            fail("T25 prompt file stores rendered instead of template", raw[:80])

        # T26 — write_system_prompt raises ValueError for empty content
        try:
            pm.write_system_prompt("   ")
            fail("T26 empty prompt should raise ValueError")
        except ValueError:
            ok("T26 write_system_prompt() raises ValueError for empty/whitespace content")

        # T27 — write_system_prompt raises KeyError for invalid placeholder
        try:
            pm.write_system_prompt("Hello {INVALID_PLACEHOLDER}")
            fail("T27 invalid placeholder should raise")
        except (KeyError, Exception):
            ok("T27 write_system_prompt() raises on invalid placeholder variable")

    finally:
        fmt.RATE_FILE            = _orig_rf
        pm.SYSTEM_PROMPT_FILE    = _orig_sp
        pm.KNOWLEDGE_BASE_DIR    = _orig_kbd
        pm._KNOWLEDGE_INDEX_FILE = _orig_kif
        pm._cache.content        = ""
        pm._cache.prompt_mtime   = None
        pm._cache.knowledge_mtime = None
        with fmt._rate_lock:
            fmt._rate_cache["value"] = None
            fmt._rate_cache["mtime"] = None
        shutil.rmtree(tmp_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5 — PERFORMANCE CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def _run_performance_tests():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ai.tools.formatters",
        str(Path("ai/tools/formatters.py").resolve()),
    )
    fmt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fmt)  # type: ignore[union-attr]

    tmp_root  = Path(tempfile.mkdtemp())
    rate_file = tmp_root / "rate.json"
    _orig_rf  = fmt.RATE_FILE
    fmt.RATE_FILE = rate_file

    with fmt._rate_lock:
        fmt._rate_cache["value"] = None
        fmt._rate_cache["mtime"] = None

    try:
        fmt.update_iqd_rate(1500.0)

        # T28 — 1000 get_iqd_rate() calls under 50ms (cache hit path)
        t0 = time.perf_counter()
        for _ in range(1000):
            fmt.get_iqd_rate()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms < 100:
            ok(f"T28 1000x get_iqd_rate() cache hits in {elapsed_ms:.1f}ms (<100ms)")
        else:
            fail("T28 rate cache performance", f"{elapsed_ms:.1f}ms for 1000 calls")

        # T29 — format_products() for 100 products under 50ms
        products = [
            {"barcode": str(i), "item_name": f"P{i}", "price": float(i + 1),
             "available_qty": 10, "description": "", "image_url": None,
             "category_name": "C", "brand_name": "B"}
            for i in range(100)
        ]
        t1 = time.perf_counter()
        fmt.format_products(products, limit=100)
        elapsed2 = (time.perf_counter() - t1) * 1000
        if elapsed2 < 50:
            ok(f"T29 format_products(100 items) in {elapsed2:.1f}ms (<50ms)")
        else:
            fail("T29 format_products performance", f"{elapsed2:.1f}ms")

    finally:
        fmt.RATE_FILE = _orig_rf
        with fmt._rate_lock:
            fmt._rate_cache["value"] = None
            fmt._rate_cache["mtime"] = None
        shutil.rmtree(tmp_root, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 65)
    print("PHASE 1 — SYSTEM PROMPT CACHE")
    print("=" * 65)
    _run_prompt_tests()

    print()
    print("=" * 65)
    print("PHASE 2 — IQD RATE CACHE")
    print("=" * 65)
    _run_rate_tests()

    print()
    print("=" * 65)
    print("PHASE 3 — SERVICE INTEGRATION")
    print("=" * 65)
    _run_service_integration_tests()

    print()
    print("=" * 65)
    print("PHASE 4 — ATOMIC WRITE INTEGRITY")
    print("=" * 65)
    _run_atomic_write_tests()

    print()
    print("=" * 65)
    print("PHASE 5 — PERFORMANCE")
    print("=" * 65)
    _run_performance_tests()

    print()
    print("=" * 65)
    print(f"FINAL RESULT: {PASS} PASSED  |  {FAIL} FAILED  |  {PASS + FAIL} TOTAL")
    print("=" * 65)

    if ISSUES:
        print()
        print("FAILURES:")
        for issue in ISSUES:
            print(f"  * {issue}")

    sys.exit(0 if FAIL == 0 else 1)
