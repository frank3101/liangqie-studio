FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# Chromium（被脚本 CDP 接管的浏览器）+ Xvfb 虚拟屏幕 + noVNC（网页里看/操作浏览器）+ ffmpeg + 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        ffmpeg \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        libglib2.0-0 \
        supervisor \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 抖音系页面会调起外部协议（想唤起抖音App/电商工作台客户端）。容器里没有关联应用，
# Chromium 会弹「Open xdg-open?」原生确认框挡住自动化。
# 实测（Debian chromium 156，容器内逐项验证）：
#   - AutoLaunchProtocolsFromOrigins 放行方案无效（合法 origins/真实点击均仍弹窗）；
#   - URLBlocklist 按 scheme 逐个拉黑（"douyin://*" 等）有效，但登录页/申诉页会触发
#     清单之外的未知 scheme，弹窗换个协议又出现 —— 修补不完；
#   - 最终方案（2026-11-21 容器内实证）：URLBlocklist ["*"] 全局拦截 + URLAllowlist
#     只放行 http/https/chrome/devtools/about，任何外部协议一律静默拦截，
#     "This page is blocked" 替代弹窗，正常网页浏览不受影响。
# 另把 xdg-open 换成空操作兜底：万一出现清单外的新协议，调起也不会有任何窗口。
RUN mkdir -p /etc/chromium/policies/managed && \
    printf '%s' '{\
"URLBlocklist":["*"],\
"URLAllowlist":[\
"http://*",\
"https://*",\
"chrome://*",\
"devtools://*",\
"about:*"],\
"ExternalProtocolDialogShowAlwaysOpenCheckbox":true}' \
    > /etc/chromium/policies/managed/zz-external-protocol.json && \
    printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/xdg-open && \
    chmod +x /usr/local/bin/xdg-open

COPY requirements.txt .
# 国内构建 pypi 直连经常被限速/断流，默认走清华镜像源（海外构建也不受影响，镜像源会自动回源）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -r requirements.txt

COPY core ./core
COPY modules ./modules
COPY server ./server
COPY web ./web
COPY assets ./assets
COPY docker/supervisord.conf /etc/supervisor/supervisord.conf

# 数据目录（挂载卷）：accounts.json、profiles、素材输出、申诉材料、state、各 json 配置都在这里
ENV DS_DATA_DIR=/data
ENV DISPLAY=:99
ENV CHROME_PATH=/usr/bin/chromium
# 剪辑用系统 ffmpeg，不用再下载
ENV IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg

RUN mkdir -p /data

EXPOSE 8000 6080 8443

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
