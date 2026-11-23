#!/usr/bin/env python3
"""HWP·HWPX 보내기 전 검사 — hwp 스킬의 기본 단계(제출·메일·업로드 전에 반드시).

  presend.py 제출본... [--form 원본양식] [--max-pages N] [--png 폴더] [--json]

파일마다 자체 엔진 hwpeng 판정(정상·주의·위험·거부)과 사유, 쪽수 셋(hwpeng 직접 배치·rhwp·파일 속 줄 배치)을 보인다.
--form 을 주면 원본 양식도 같이 재서 나빠졌는지 본다. --png 는 rhwp 로 쪽 그림을 뽑는다(실명 최종본에는 쓰지 않는다).

종료값: 0 보내도 됨 · 1 멈추고 확인(위험, 알 수 없는 주의, 쪽수 초과·못 잼, 원본 양식이 거부·위험·읽기 실패)
        · 2 보내지 않음(보낼 파일이 거부·읽기 실패)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ENGINE = os.path.expanduser(os.environ.get('HWPENG_HOME', '~/projects/hwp-engine'))
sys.path.insert(0, ENGINE)
from hwpeng.__main__ import _quiet, check_one  # noqa: E402

# 보내도 되는 「주의」: 줄 배치 없음(hwp-cli·hwpx_fill 결과물, 한컴이 열 때 다시 배치), 요약 정보 스트림 없음
BENIGN = {'no_line_seg', 'stream'}


def rhwp_pages(path):
    if not shutil.which('rhwp'):
        return None
    try:
        r = subprocess.run(['rhwp', 'dump-pages', path], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    m = re.search(r'\((\d+)페이지\)', r.stdout)
    return int(m.group(1)) if m else None


def measure(path):
    res = check_one(path)
    res['rhwp_pages'] = rhwp_pages(path) if res['verdict'] != '거부' else None
    return res


def safe_measure(path):
    """엔진이 못 읽는 파일(없는 경로 포함)도 결과로 남긴다 — 제출본이면 「보내지 않음」."""
    try:
        return measure(path)
    except Exception as ex:
        return {'file': path, 'verdict': '읽기 실패', 'issues': [], 'error': f'{type(ex).__name__}: {ex}', 'rhwp_pages': None}


def judge(res, form, max_pages):
    """(종료값, [사유])."""
    v = res['verdict']
    why = []
    if v in ('거부', '읽기 실패'):
        return 2, [f'{v}: 보내지 않는다']
    if v == '위험':
        why.append('위험: 보내기 전에 사유를 고치거나 사장에게 묻는다')
    odd = [i['code'] for i in res['issues'] if i['level'] == 'W' and i['code'] not in BENIGN]
    if odd:
        why.append(f'확인할 주의: {", ".join(odd)}')
    counts = [c for c in (res.get('pages'), res['rhwp_pages']) if c]
    if len(set(counts)) > 1:
        why.append(f'쪽수 불확실(hwpeng {res.get("pages")} · rhwp {res["rhwp_pages"]}) — 제한은 큰 값으로 판단')
    elif len(counts) < 2:
        why.append(f'쪽수 교차 확인 없음(hwpeng {res.get("pages")} · rhwp {res["rhwp_pages"]})')
    over = bool(max_pages) and (not counts or max(counts) > max_pages)   # 제한이 있는데 못 쟀으면 멈춘다
    if over:
        why.append(f'쪽수 초과: 최대 {max(counts)}쪽 > 제한 {max_pages}쪽' if counts else f'쪽수를 재지 못해 제한 {max_pages}쪽을 확인할 수 없음')
    bad_form = form is not None and form['verdict'] not in ('정상', '주의')
    if bad_form:
        why.append(f'원본 양식이 {form["verdict"]} — 주최 원본이 맞는지, 경로가 맞는지 먼저 본다')
    elif form and v == '위험':
        why.append(f'원본 양식은 {form["verdict"]}인데 제출본은 {v} — 만드는 과정에서 생긴 문제')
    stop = v == '위험' or odd or over or bad_form
    return (1 if stop else 0), why


def png(path, outdir):
    """rhwp 쪽 그림을 이번 실행 전용 하위 폴더에 뽑는다(앞 실행의 쪽이 섞이지 않게). 그림 경로 목록."""
    os.makedirs(outdir, exist_ok=True)
    name = os.path.splitext(os.path.basename(path))[0]
    d = tempfile.mkdtemp(prefix=name + '_', dir=outdir)
    stem = os.path.join(d, name)
    subprocess.run(['rhwp', 'export-pdf', path, '-o', stem + '.pdf'], check=True, capture_output=True, timeout=300)
    subprocess.run(['pdftoppm', '-r', '70', '-png', stem + '.pdf', stem], check=True, timeout=300)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith('.png'))


def main(argv=None):
    _quiet()
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('files', nargs='+')
    ap.add_argument('--form', help='채우기 전 원본 양식(주최가 준 파일)')
    ap.add_argument('--max-pages', type=int, help='요강의 쪽수 제한')
    ap.add_argument('--png', metavar='폴더', help='rhwp 로 쪽 PNG 를 뽑을 폴더')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)

    form = safe_measure(a.form) if a.form else None
    out, worst = [], 0
    for f in a.files:
        res = safe_measure(f)
        res['exit'], res['why'] = judge(res, form, a.max_pages)
        if a.png and res['exit'] < 2:
            res['png'] = png(f, a.png)
        worst = max(worst, res['exit'])
        out.append(res)
    if a.json:
        print(json.dumps({'form': form, 'files': out}, ensure_ascii=False, indent=1))
        return worst
    if form:
        print(f'원본 양식 [{form["verdict"]}] {form["file"]} · hwpeng {form.get("pages")}쪽 · rhwp {form["rhwp_pages"]}쪽'
              f' · 파일 속 줄 배치 {form.get("hancom_cache_pages")}쪽' + (f' — {form["error"]}' if form.get('error') else ''))
    for r in out:
        tag = {0: '보내도 됨', 1: '멈추고 확인', 2: '보내지 않음'}[r['exit']]
        print(f'[{r["verdict"]} → {tag}] {r["file"]}')
        if r['verdict'] not in ('거부', '읽기 실패'):
            print(f'   쪽수: hwpeng {r.get("pages")} · rhwp {r["rhwp_pages"]} · 파일 속 줄 배치 {r.get("hancom_cache_pages")}')
        for it in r['issues']:
            lv = {'F': '거부', 'E': '위험', 'W': '주의'}[it['level']]
            print(f'   {lv} {it["code"]} ×{it["count"]}: {it["examples"][0]}')
        for w in r['why']:
            print(f'   → {w}')
        if r.get('error') or r.get('layout_error'):
            print(f'   {r.get("error") or r.get("layout_error")}')
        if r.get('png'):
            print(f'   PNG {len(r["png"])}장: {a.png}')
    return worst


if __name__ == '__main__':
    sys.exit(main())
