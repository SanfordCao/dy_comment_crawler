# 抖音一级评论抓取器

Python 3.12 + Playwright 实现。程序默认启动一个专用的系统 Chrome 配置，通过正常页面滚动触发评论懒加载，保存一级评论文字和评论图片。

## 功能

- 只读取评论列表响应最外层的 `comments` 数组；
- 不读取 `reply_comment`，不抓取二级回复接口；
- 默认持续向下滚动加载更多，直到接口返回 `has_more=false` 或连续多轮没有新增；
- 每轮把鼠标移动到距窗口右边缘 150px、垂直居中的评论区探测点，再向上寻找最近的滚动容器；
- 保存评论文字、作者、时间、IP 标签、点赞数、回复数；
- 每条评论的图片顺序下载，并在 JSON 中记录对应文件名；
- 每次执行创建新的时间戳目录，不覆盖以前的数据；
- 使用专用持久化 Chrome 配置，首次人工登录后可在后续运行中复用；
- 遇到登录、滑块或安全验证时暂停，由用户手动处理，不绕过验证。

## 输出格式

```text
output/
└── 20260731-153000-7666731321595551205/
    ├── comments.json
    ├── comment-123456-01.jpg
    └── comment-789012-01.jpg
```

`comments.json` 中每条评论包含：

- `id`、`aweme_id`、`text`；
- `author`；
- `create_time`、`create_time_iso`、`ip_label`；
- `likes`、`reply_count`；
- `source_image_urls`：评论接口中的原始图片地址；
- `images`：下载后的文件名、原地址、类型和字节数。

图片下载失败时，`file` 为 `null`，并保留错误原因。

抖音返回的 WebP 评论图片会自动转换为质量 92 的 JPEG；透明区域使用白色背景，方便直接通过系统预览或 PyCharm 查看。

## 环境

项目明确要求 Python 3.12：

```bash
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

程序使用系统已安装的 Google Chrome，不需要执行 `playwright install chromium`。

## 首次运行与登录

默认会创建 `.douyin-browser-profile/` 并打开一个新的 Chrome 窗口。首次运行时在这个窗口中手动登录抖音；登录状态会保留到后续运行。不要同时启动两个使用同一配置目录的抓取进程。

## 在 PyCharm 中运行

1. 将项目解释器设置为：

   ```text
   /opt/miniconda3/envs/ai_application/bin/python
   ```

2. Script path 选择：

   ```text
   /Users/caoyuan/workplace/dy_comment_crawler/src/douyin_comment_crawler/__main__.py
   ```

3. Working directory 设置为：

   ```text
   /Users/caoyuan/workplace/dy_comment_crawler
   ```

4. 直接点击 Run。没有填写 Parameters 时，Run 控制台会提示：

   ```text
   请输入抖音视频 URL，然后按回车：
   ```

   粘贴 URL 即可，不会再因为缺少 `--url` 退出。

也可以在 Run Configuration → Parameters 中预填：

```text
--url "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7666731321595551205&showTab=like"
```

首次运行会打开专用 Chrome。登录完成后，回到 PyCharm Run 控制台按回车，程序才会继续。默认给登录或验证留出 30 分钟；需要更长时间可在 Parameters 中增加：

```text
--challenge-wait-minutes 60
```

## 命令行运行（可选）

```bash
douyin-comments \
  --url 'https://www.douyin.com/user/self?from_tab_name=main&modal_id=7666731321595551205&showTab=like'
```

也可以直接运行模块：

```bash
python -m douyin_comment_crawler \
  --url 'https://www.douyin.com/video/7666731321595551205'
```

根据参考脚本，默认向下滚动以加载更多。如果某个抖音页面版本需要向上加载：

```bash
douyin-comments \
  --url 'https://www.douyin.com/video/7666731321595551205' \
  --scroll-direction up
```

查看全部参数：

```bash
douyin-comments --help
```

高级用法：如果确实需要连接已经开启远程调试的 Chrome，可传入 CDP 地址：

```bash
douyin-comments \
  --url '<抖音地址>' \
  --cdp 'ws://127.0.0.1:9222/devtools/browser/<id>'
```

不建议把日常使用且打开大量标签页的 Chrome 作为默认目标，因为 Playwright 会初始化其中所有页面，连接可能很慢。默认专用配置不会读取无关标签页。

常用限制参数：

```bash
douyin-comments \
  --url '<抖音地址>' \
  --max-comments 100 \
  --max-minutes 15 \
  --min-delay-ms 1800 \
  --max-delay-ms 3200
```

## 风控原则

- 默认单视频、单标签页、顺序滚动和顺序下载，不并发请求。
- 程序通过 `elementFromPoint(innerWidth - 150, innerHeight / 2)` 获取评论区中的实际元素。
- 从该元素逐层向上寻找 `overflow-y` 为 `auto`、`scroll` 或 `overlay` 且内容确实溢出的最近容器。
- 每轮同时更新该容器的 `scrollTop` 并向它发送合成 `WheelEvent`，不调用页面级 `page.mouse.wheel()`。
- 找不到带滚动条的容器时会跳过本轮，不执行模糊兜底。
- 每次滚动随机等待 1.2–2.2 秒；账号频繁验证时应调大间隔。
- 不主动构造评论翻页请求，只监听页面正常滚动产生的响应。
- 不绕过验证码、滑块、登录限制或账号风险验证。
- 首次登录和后续安全验证默认等待 30 分钟。
- 默认最多运行 30 分钟，连续 8 轮无新增后停止。
- 失败时仍会保留本次独立目录和 `comments.json`，方便定位问题。
- 请只采集你有权访问的公开内容，并遵守平台规则与适用法律。

建议先用少量评论的视频验证。若频繁触发验证，应立即停止并降低执行频率。

## 测试

```bash
python -m compileall -q src tests
python -m unittest discover -s tests -v
```

## 本地查看评论和图片

在 PyCharm 中新建一个 Python Run Configuration：

```text
Script path:
/Users/caoyuan/workplace/dy_comment_crawler/src/douyin_comment_crawler/viewer.py

Working directory:
/Users/caoyuan/workplace/dy_comment_crawler
```

直接运行后会自动打开“抖音评论本地查看器”。点击“选择评论目录”，选择某次
抓取生成的目录，例如：

```text
output/20260731-173508-7666731321595551205/
```

页面会读取其中的 `comments.json`，显示评论文字、作者、时间、地区、点赞、
回复数和对应的本地图片。支持搜索、仅看带图评论、排序和图片放大预览。
文件仅在浏览器本地读取，不会上传。
