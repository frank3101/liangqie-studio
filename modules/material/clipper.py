# -*- coding: utf-8 -*-
"""自动剪辑：毛料(视频-N.mp4) -> 模板装配 -> 成品。

用 ffmpeg 直接实现（原 auto_cut 流水线已不可用）：
  * 模板 = 固定片头/中段(取自模板原片) + 产品槽(用毛料最清晰的片段填充)
  * 毛料分析：清晰度(拉普拉斯方差) + 运动量(相邻采样帧灰度差) + 人脸占比
    - 静止画面(图片轮播)拒用、主播出镜拒用（可开关）
  * 音频取模板原片原样复制（与原流水线一致）
输出: <商品>/成品/成品_{模板名}_{序号}.mp4 (720x1280, 与模板总帧数一致)
"""
import glob
import json
import os
import platform
import random
import subprocess
import sys
import tempfile
from pathlib import Path

# 直接以脚本方式运行时（python clipper.py ...）把项目根目录加进 sys.path
_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import cv2
import imageio_ffmpeg

from modules.material import store

FF = imageio_ffmpeg.get_ffmpeg_exe()
W, H, FPS = 720, 1280, 30
CHUNK_MAX = 240          # 单个填充片段最多帧数(8s)
STILL_REJECT = 2.0       # 运动量低于此值视为静止(图片轮播)
FACE_REJECT_RATIO = 0.5  # 采样帧中人脸占比超过此值视为主播出镜
VF = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
      f"crop={W}:{H},setsar=1,fps={FPS}")

_face_cascade = None


def log_default(m):
    print(m, flush=True)


def _log(msg, log=None):
    (log or log_default)(msg)


def _run(args, timeout=900):
    extra = {"creationflags": 0x08000000} if platform.system() == "Windows" else {}
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore",
                       timeout=timeout, **extra)
    return r


def _cascade():
    global _face_cascade
    if _face_cascade is None:
        p = None
        try:
            p = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            c = cv2.CascadeClassifier(p)
        except Exception:
            c = cv2.CascadeClassifier()
        # 兜底：打包环境里 cv2.data 丢失时从 _MEIPASS / 本地找；再找不到就返回空分类器
        if c.empty():
            import sys
            from pathlib import Path as _P
            for base in (getattr(sys, "_MEIPASS", None), _P(__file__).parent.parent.parent):
                if not base:
                    continue
                for cand in (_P(base) / "cv2" / "data" / "haarcascade_frontalface_default.xml",
                             _P(base) / "assets" / "haarcascade_frontalface_default.xml"):
                    if cand.exists():
                        c = cv2.CascadeClassifier(str(cand))
                        break
                if not c.empty():
                    break
        _face_cascade = c if not c.empty() else False   # False = 不可用，跳过人脸检测
    return _face_cascade if _face_cascade is not False else None


def analyze_video(path):
    """返回 dict: dur(秒), sharp(0~1 归一), motion, face_ratio, usable, reason"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {"usable": False, "reason": "打不开"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if n <= 0:
        cap.release()
        return {"usable": False, "reason": "读不到帧数"}
    dur = n / float(fps or 30)
    # 采样: 跳过首尾 6%，取最多 12 帧
    k = min(12, max(4, int(dur)))
    idxs = [int(n * (0.06 + 0.88 * i / max(1, k - 1))) for i in range(k)]
    idxs = [min(i, n - 1) for i in idxs]
    sharp, motion, faces, prev = [], [], 0, None
    for fi in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(g, (160, 284))
        sharp.append(cv2.Laplacian(small, cv2.CV_64F).var())
        if prev is not None:
            motion.append(float(cv2.absdiff(small, prev).mean()))
        prev = small
        fs = cv2.resize(g, (320, 568))
        casc = _cascade()
        if casc is not None:
            rects = casc.detectMultiScale(fs, 1.15, 4, minSize=(60, 60))
            if len(rects):
                faces += 1
    cap.release()
    if not sharp:
        return {"usable": False, "reason": "采样失败"}
    res = {
        "path": str(path), "dur": dur, "frames": n, "fps": fps,
        "sharp": sorted(sharp)[len(sharp) // 2],       # 中位数更抗噪
        "motion": sum(motion) / len(motion) if motion else 0.0,
        "face_ratio": faces / len(idxs),
        "usable": True, "reason": "",
    }
    return res


def filter_materials(analyses, reject_face=True, reject_still=True, log=None):
    """按规则过滤 + 打分排序，返回 (usable列表, rejected列表)"""
    usable, rejected = [], []
    for a in analyses:
        if not a.get("usable"):
            rejected.append((a, a.get("reason")))
            continue
        if reject_still and a["motion"] < STILL_REJECT:
            a["usable"] = False
            rejected.append((a, f"静止画面(运动量{a['motion']:.1f})"))
            continue
        if reject_face and a["face_ratio"] > FACE_REJECT_RATIO:
            a["usable"] = False
            rejected.append((a, f"主播出镜(人脸{a['face_ratio'] * 100:.0f}%)"))
            continue
        usable.append(a)
    # 打分: 清晰度归一后 70% + 运动量归一 30%
    if usable:
        smax = max(u["sharp"] for u in usable) or 1.0
        mmax = max(u["motion"] for u in usable) or 1.0
        for u in usable:
            u["score"] = 0.7 * (u["sharp"] / smax) + 0.3 * (u["motion"] / mmax)
        usable.sort(key=lambda x: -x["score"])
    return usable, rejected


def _render_chunk(src, start, frames, out_path):
    """从 src 的 start 秒开始取 frames 帧，统一规格输出"""
    dur = frames / float(FPS) + 0.5
    args = [FF, "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-ss", f"{start:.3f}", "-i", str(src),
            "-t", f"{dur:.3f}",
            "-vf", VF, "-frames:v", str(frames),
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(out_path)]
    r = _run(args)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg 分段失败: {r.stderr[-300:]}")


def _count_frames(path):
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    return n


def _fixed_chunk(source, start_sec, frames, out_path):
    """模板原片的固定段"""
    args = [FF, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_sec:.4f}", "-i", str(source),
            "-vf", VF, "-frames:v", str(frames),
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(out_path)]
    r = _run(args)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"固定段抽取失败: {r.stderr[-300:]}")


MIN_CHUNK = 45           # 片段最短 1.5s，太碎没意义


def _new_cursor(usable, rng):
    """每条毛料的读取游标（随机起始偏移，避免每版成片都从同一帧开头）"""
    cur = {}
    for a in usable:
        head = min(2.0, max(0.0, a["dur"] * 0.15))
        cur[a["path"]] = rng.uniform(0.0, head) if head > 0.05 else 0.0
    return cur


def _take_segment(usable, want, need, rng, cur, avoid=None):
    """从毛料带上按顺序取一段连续画面（互不重叠、不留碎片）。

    按游标从小到大挑毛料（相当于轮转使用不同素材），返回 (毛料, 起点, 帧数)。
    """
    order = sorted(usable, key=lambda a: (cur[a["path"]], -a.get("score", 0.0)))
    for allow_avoid in (False, True):     # 先避开上一段用过的，实在没有再用
        for a in order:
            if a["path"] == avoid and not allow_avoid:
                continue
            room = int((a["dur"] - cur[a["path"]] - 0.1) * FPS)
            if room < MIN_CHUNK:
                continue
            take = min(want, need, room)
            # 剩下的不够一段就一次用完，别留碎片
            if take < need and room - take < MIN_CHUNK * 2:
                take = min(room, need)
            return a, cur[a["path"]], take
    return None, 0.0, 0


def _plan_slot(usable, frames, rng, cur, avoid=None):
    """给一个产品槽排片段计划: [(video_path, start_sec, frames), ...]

    块长 3~8s；跨槽共享游标，同一段画面不会被两个槽重复使用；
    素材带用完才会回到开头复用（并错开起点，避免同一画面紧挨着出现）。
    """
    plan, need, guard, last = [], frames, 0, avoid
    if not usable:
        return plan
    while need > 0 and guard < 200:
        guard += 1
        want = min(need, rng.choice([90, 120, 150, 180, 240]))
        a, start, chunk = _take_segment(usable, want, need, rng, cur, avoid=last)
        if a is None:                     # 素材带用完了 -> 回卷复用
            for x in usable:
                cur[x["path"]] = rng.uniform(0.0, max(0.0, x["dur"] * 0.3))
            a, start, chunk = _take_segment(usable, want, need, rng, cur, avoid=last)
        if a is None:                     # 极端情况：素材短到装不下
            a = usable[0]
            chunk = min(need, want)
            start = rng.uniform(0.0, max(0.0, a["dur"] - 0.5))
        cur[a["path"]] = start + chunk / float(FPS) + 0.25   # 段间留 0.25s，避免接缝重复
        plan.append((a["path"], start, chunk))
        last = a["path"]
        need -= chunk
    return plan


def load_template(name):
    tpl_path = store.ASSETS / "templates" / f"{name}.json"
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    # 模板原片: source 相对路径按 assets 根解析（兼容旧相对写法）
    src = tpl.get("source", "")
    cands = []
    if src:
        cands += [store.ASSETS / src,
                  store.ASSETS.parent / src,
                  store.ASSETS / "原片" / Path(src).name]
    source = next((c for c in cands if c.is_file()), None)
    if source is None:
        raise FileNotFoundError(f"模板原片找不到: {src}")
    return tpl_path, tpl, source


def clips_done(out_dir, count=1):
    d = Path(out_dir) / "成品"
    if not d.is_dir():
        return 0
    return len(glob.glob(str(d / "成品_*.mp4")))


def clip_product(pid, out_dir, template=None, count=1,
                 reject_face=None, reject_still=None, log=None,
                 force=False, analyses=None):
    """给一个商品出成品。返回成品路径列表；无毛料/全被拒时返回 []。

    reject_face / reject_still 传 None 时读 material.json 里的配置。
    """
    _log(f"[{pid}] 剪辑开始", log)
    out_dir = Path(out_dir)
    vids = sorted(out_dir.glob("视频-*.mp4"))
    if not vids:
        _log(f"  x 没有 毛料(视频-N.mp4)", log)
        return []
    return _clip(pid, out_dir, vids, template, count, reject_face, reject_still,
                 log, force, analyses)


def _clip(pid, out_dir, vids, template, count, reject_face, reject_still,
          log, force, analyses=None):
    cfg = store.load()
    clip_cfg = cfg.get("clip", {})
    tpl_name = template or clip_cfg.get("template") or "贾乃亮精选好物"
    count = int(count or clip_cfg.get("count", 1))
    if reject_face is None:
        reject_face = clip_cfg.get("reject_face", True)
    if reject_still is None:
        reject_still = clip_cfg.get("reject_still", True)
    tpl_path, tpl, source = load_template(tpl_name)
    final_dir = out_dir / "成品"
    final_dir.mkdir(exist_ok=True)

    # 已有成品且够数: 跳过
    if not force and clips_done(out_dir, count) >= count:
        _log(f"  已有 {clips_done(out_dir, count)} 条成品, 跳过（force 可重剪）", log)
        return sorted(final_dir.glob("成品_*.mp4"))

    # 分析毛料
    if analyses is None:
        analyses = []
        for v in vids:
            a = analyze_video(v)
            analyses.append(a)
            _log(f"  毛料 {v.name}: {a['dur']:.0f}s 清晰度{a.get('sharp', 0):.0f} "
                 f"运动{a.get('motion', 0):.1f} 人脸{a.get('face_ratio', 0) * 100:.0f}%", log)
    usable, rejected = filter_materials(analyses, reject_face, reject_still, log)
    for a, why in rejected:
        _log(f"  - 拒用 {Path(a['path']).name if a.get('path') else '?'}: {why}", log)
    if not usable:
        _log(f"  x 全部毛料被拒用, 出不了片", log)
        return []
    _log(f"  可用毛料 {len(usable)} 条", log)

    # 素材够不够：模板产品槽总时长 vs 毛料总时长（不够就必然出现画面复用）
    need_sec = sum(int(s.get("frames", 0)) for s in tpl.get("segments", [])
                   if s.get("type") != "fixed") / float(FPS)
    have_sec = sum(a["dur"] for a in usable)
    if have_sec < need_sec:
        _log(f"  ! 素材偏少：需要约{need_sec:.0f}s、可用{have_sec:.0f}s，"
             f"会有片段复用；建议每商品多采几条毛料", log)
    elif have_sec < need_sec * 1.5:
        _log(f"  素材 {have_sec:.0f}s / 需求 {need_sec:.0f}s，够用但不宽裕", log)

    outs = []
    for k in range(count):
        seq = clips_done(out_dir, count) + 1 if force else k + 1
        final = final_dir / f"成品_{tpl_name}_{seq:02d}.mp4"
        if final.exists() and not force:
            outs.append(final)
            continue
        rng = random.Random(f"{pid}-{k}-{os.urandom(2).hex()}")
        try:
            _render_one(tpl, source, usable, final, rng, log)
            outs.append(final)
        except Exception as e:
            _log(f"  x 成片失败({k + 1}/{count}): {e}", log)
    # 成品清单写进商品资料，发布模块直接取
    try:
        from modules.material import productinfo
        productinfo.save(out_dir, {"clips": [p.name for p in outs],
                                   "clip_template": tpl_name})
    except Exception:
        pass
    return outs


def _render_one(tpl, source, usable, final, rng, log):
    work = Path(tempfile.mkdtemp(prefix="clip_"))
    try:
        parts, total = [], 0
        si = 0
        # 全局素材游标: 跨槽共享，保证各槽用的画段互不重复
        cur = _new_cursor(usable, rng)
        last_src = None       # 上一段用的毛料，跨槽衔接处避免同源
        for seg in tpl.get("segments", []):
            frames = int(seg.get("frames", 0))
            if seg.get("type") == "fixed":
                p = work / f"seg{si:02d}.mp4"
                start = float(seg.get("start", 0.0))
                _fixed_chunk(source, start, frames, p)
                _log(f"  固定段[{seg.get('label', si)}] {frames}帧", log)
            else:
                plan = _plan_slot(usable, frames, rng, cur, avoid=last_src)
                p = work / f"seg{si:02d}.mp4"
                chunks = []
                ci = 0
                for (vpath, start, cframes) in plan:
                    cp = work / f"seg{si:02d}_c{ci:02d}.mp4"
                    _render_chunk(vpath, start, cframes, cp)
                    n = _count_frames(cp)
                    if n < cframes:
                        # 少帧: 补一段循环片段
                        cp2 = work / f"seg{si:02d}_c{ci:02d}b.mp4"
                        _render_chunk(vpath, 0.0, cframes - n, cp2)
                        chunks += [cp, cp2]
                    else:
                        chunks.append(cp)
                    _log(f"    片段{ci + 1}: {Path(vpath).name} "
                         f"@{start:.1f}s +{cframes / float(FPS):.1f}s", log)
                    ci += 1
                if chunks:
                    _concat(chunks, p)
                else:
                    raise RuntimeError(f"产品槽[{seg.get('name', si)}]无可填素材")
                if plan:
                    last_src = plan[-1][0]
                _log(f"  产品槽[{seg.get('name', si)}] {frames}帧 "
                     f"({len(plan)}个片段)", log)
            parts.append(p)
            total += frames
            si += 1

        merged = work / "video.mp4"
        _concat(parts, merged)
        dur = total / float(FPS)
        # 音频取模板原片原样
        args = [FF, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(merged), "-i", str(source),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-ar", "44100",
                "-t", f"{dur:.3f}", "-movflags", "+faststart", str(final)]
        r = _run(args)
        if r.returncode != 0 or not final.exists():
            raise RuntimeError(f"音频合成失败: {r.stderr[-300:]}")
        _log(f"  v 成品 {final.name} "
             f"({final.stat().st_size / 1048576:.1f}MB, {dur:.1f}s)", log)
    finally:
        for p in sorted(work.rglob("*"), reverse=True):
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass


def _concat(files, out_path):
    lst = out_path.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(f).resolve().as_posix()}'\n" for f in files),
                   encoding="utf-8")
    r = _run([FF, "-y", "-hide_banner", "-loglevel", "error",
              "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
              str(out_path)])
    lst.unlink(missing_ok=True)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"拼接失败: {r.stderr[-300:]}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--template", default="")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = Path(args.out_dir)
    pid = d.name
    res = _clip(pid, d, sorted(d.glob("视频-*.mp4")), args.template or None,
                args.count, None, None, None, args.force)
    print(f"完成: {len(res)} 条成品")
