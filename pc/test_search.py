# -*- coding: utf-8 -*-
"""搜索链路自检。需要联网，不需要登录。

    python test_search.py

检查五件事：
  1. 正常搜索能拿到结果
  2. 同一关键词第二次命中缓存（不发请求）
  3. 主接口被风控（-111）时自动降级到老接口
  4. 所有通道都失败时，错误文案是给人看的，不是 csrf 术语
  5. 老接口返回的 play 等字段是字符串，转 int 后排序不崩
"""
import sys
import time

import bili

KW = "自检%d" % int(time.time())     # 带时间戳，避免撞上已有缓存


def urls_of(calls):
    return [u[1] if isinstance(u, tuple) else u for u in calls]


def main():
    c = bili.BiliClient()            # 匿名即可，搜索不需要登录
    real_get = bili.BiliClient._get
    calls = []

    def spy(self, url, params=None, signed=False, timeout=15, headers=None, session=None):
        calls.append(url)
        return real_get(self, url, params=params, signed=signed, timeout=timeout,
                        headers=headers, session=session)

    def fail_wbi(self, url, params=None, signed=False, timeout=15, headers=None, session=None):
        calls.append(("WBI-FAIL", url))
        if url == bili.SEARCH_WBI_URL:
            return {"code": -111, "message": "csrf 校验失败"}
        return real_get(self, url, params=params, signed=signed, timeout=timeout,
                        headers=headers, session=session)

    def fail_all(self, url, params=None, signed=False, timeout=15, headers=None, session=None):
        calls.append(("FAKE", url))
        if "search" in url:
            return {"code": -111, "message": "csrf 校验失败"}
        return real_get(self, url, params=params, signed=signed, timeout=timeout,
                        headers=headers, session=session)

    print("1 正常搜索")
    bili.BiliClient._get = spy
    calls.clear()
    r = c.search(KW)
    assert r and r[0]["bvid"].startswith("BV"), "没拿到结果"
    print("   结果 %d 条" % len(r))

    print("2 缓存命中")
    calls.clear()
    r2 = c.search(KW)
    assert not any(u == bili.SEARCH_WBI_URL for u in calls), "缓存没生效，又发了请求"
    assert [x["bvid"] for x in r2] == [x["bvid"] for x in r], "缓存内容不一致"
    print("   第二次 0 请求，结果一致")

    print("3 主接口 -111 时降级")
    bili.BiliClient._get = fail_wbi
    calls.clear()
    r3 = c.search(KW + "b")
    assert r3, "降级后没拿到结果"
    assert bili.SEARCH_LEGACY_URL in urls_of(calls), "没走老接口"
    print("   走了老接口，拿到 %d 条" % len(r3))

    print("4 全部失败时的文案")
    bili.BiliClient._get = fail_all
    try:
        c.search(KW + "c")
        raise AssertionError("应该报错却没有")
    except bili.BiliError as e:
        assert "风控" in str(e) and "csrf" not in str(e).lower(), "文案还是技术术语：%s" % e
        print("   %s" % e)

    print("5 老接口字段类型")
    bili.BiliClient._get = fail_wbi
    r5 = c.search(KW + "d", order="click")
    assert r5 and all(isinstance(x["play"], int) for x in r5), "play 不是 int，排序会崩"
    print("   按播放量排序正常")

    bili.BiliClient._get = real_get
    print("\n全部通过")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("失败：%s" % e)
        sys.exit(1)
