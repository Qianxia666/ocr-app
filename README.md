## 二开优化

- 前端页面优化，响应式布局，可在前台查看日志。
- 用户意外退出后，系统自动停止该用户的任务。
- 新增INVITE_CODE变量代替PASSWORD变量，可以在前端输入后进入网站。

## 环境变量

| 环境变量名         | 描述                           | 默认值                         |
|--------------------|--------------------------------|--------------------------------|
| `API_BASE_URL`     | API 请求地址                   | `https://api.openai.com`      |
| `OPENAI_API_KEY`   | OpenAI API 密钥                | `sk-111111111`                |
| `MODEL`            | 使用的模型名称                 | `gpt-4o`                      |
| `CONCURRENCY`      | 并发限制 (最大并发数)          | `5`                           |
| `MAX_RETRIES`      | 最大重试次数                   | `5`                           |
| `FAVICON_URL`      | 网站的图标 URL                 | `/static/favicon.ico`         |
| `TITLE`            | 网站标题                       | `OCR图像转文本`              |
| `INVITE_CODE`      | 网站访问邀请码                 | `无`                          |
| `BACK_URL`         | 服务后端代码，设置成https的不过cloudflare的反代域名，能解决cloudflare 100秒请求超时的限制,不设置就获取你网页当前窗口的域名或ip| |

## docker-compose.yaml
```
version: '3.8'

services:
  ocr-app:
    image: houyinx/ocr-app:latest
    environment:
      - API_BASE_URL=https://api.openai.com
      - OPENAI_API_KEY=sk-111111111
    ports:
      - "54188:54188"
    restart: always
    network_mode: bridge
```
