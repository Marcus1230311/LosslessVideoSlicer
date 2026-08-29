from __future__ import annotations
import json, os, subprocess, hashlib, tempfile
from pathlib import Path
from bisect import bisect_left

VIDEO_EXTS={'.mp4','.mov','.mkv','.webm','.avi','.m4v','.ts','.m2ts','.mts'}

def run_hidden(cmd, **kw):
    if os.name == 'nt':
        kw.setdefault('creationflags', 0x08000000)
    return subprocess.run(cmd, **kw)

def probe_media(ffprobe: str, path: str):
    p=run_hidden([ffprobe,'-v','error','-show_entries','format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate','-of','json',path],capture_output=True,text=True,check=True)
    data=json.loads(p.stdout)
    fmt=data.get('format',{})
    return {
        'duration_ms': int(float(fmt.get('duration') or 0)*1000),
        'size': int(fmt.get('size') or os.path.getsize(path)),
        'bit_rate': int(fmt.get('bit_rate') or 0),
        'streams': data.get('streams',[])
    }

def probe_keyframes(ffprobe: str, path: str):
    # packet flags are faster than decoding frames and sufficient for stream-copy boundaries
    p=run_hidden([ffprobe,'-v','error','-select_streams','v:0','-show_entries','packet=pts_time,flags','-of','csv=p=0',path],capture_output=True,text=True,check=True)
    out=[]
    for line in p.stdout.splitlines():
        parts=[x.strip() for x in line.split(',')]
        if len(parts)<2: continue
        try: t=float(parts[0])
        except: continue
        flags=parts[1]
        if 'K' in flags:
            out.append(max(0,int(round(t*1000))))
    if not out or out[0] > 50: out.insert(0,0)
    return sorted(set(out))

def nearest_keyframe(keyframes, ms):
    if not keyframes: return int(ms)
    i=bisect_left(keyframes, int(ms))
    if i<=0: return keyframes[0]
    if i>=len(keyframes): return keyframes[-1]
    a,b=keyframes[i-1],keyframes[i]
    return a if abs(ms-a)<=abs(b-ms) else b

def next_keyframe(keyframes, ms):
    if not keyframes: return int(ms)
    i=bisect_left(keyframes,int(ms))
    if i>=len(keyframes): return keyframes[-1]
    return keyframes[i]

def previous_keyframe(keyframes, ms):
    if not keyframes: return int(ms)
    i=bisect_left(keyframes,int(ms))
    if i<len(keyframes) and keyframes[i]==int(ms): return keyframes[i]
    return keyframes[max(0,i-1)]

def cache_dir_for(path: str):
    st=os.stat(path)
    key=f'{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}'.encode('utf-8','ignore')
    h=hashlib.sha1(key).hexdigest()[:16]
    base=Path(os.getenv('LOCALAPPDATA') or tempfile.gettempdir())/'LosslessSlicer'/'thumbs'/h
    base.mkdir(parents=True,exist_ok=True)
    return base

def generate_thumbnails(ffmpeg: str, path: str, duration_ms: int, count=96):
    cache=cache_dir_for(path)
    existing=sorted(cache.glob('thumb_*.jpg'))
    if len(existing)>=min(count,12): return [str(x) for x in existing]
    for p in existing:
        try:p.unlink()
        except:pass
    duration=max(0.1,duration_ms/1000)
    fps=max(0.02,count/duration)
    out=str(cache/'thumb_%04d.jpg')
    cmd=[ffmpeg,'-hide_banner','-loglevel','error','-i',path,'-vf',f'fps={fps:.8f},scale=180:-2','-frames:v',str(count),'-q:v','4','-y',out]
    run_hidden(cmd,capture_output=True,check=True)
    return [str(x) for x in sorted(cache.glob('thumb_*.jpg'))]


def generate_waveform(ffmpeg: str, path: str, width=2400, height=92):
    cache=cache_dir_for(path)
    out=cache/'waveform.png'
    if out.exists() and out.stat().st_size>1000:
        return str(out)
    # Transparent-ish waveform is composited/tinted by the Qt timeline painter.
    cmd=[ffmpeg,'-hide_banner','-loglevel','error','-i',path,'-filter_complex',
         f'aformat=channel_layouts=mono,showwavespic=s={int(width)}x{int(height)}:colors=White:scale=sqrt',
         '-frames:v','1','-y',str(out)]
    run_hidden(cmd,capture_output=True,check=True)
    return str(out) if out.exists() else ''


def lossless_safe_bounds(keyframes, start_ms, end_ms, duration_ms):
    """Resolve arbitrary edit points to stream-copy-safe keyframe bounds.

    Start expands backward to the previous keyframe; end expands forward to the
    next keyframe. The editor model itself is never changed.
    """
    a=max(0,int(start_ms)); b=min(int(duration_ms),int(end_ms))
    if not keyframes:
        return a,b
    sa=max(0, previous_keyframe(keyframes,a))
    eb=min(int(duration_ms), next_keyframe(keyframes,b))
    if eb <= sa:
        eb=min(int(duration_ms), max(sa+1,b))
    return sa,eb
