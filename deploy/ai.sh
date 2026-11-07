#!/bin/sh
# AI 서버(EC2): Ollama. 채팅 모델 safety-chat 은 ai/export_gguf.sh 결과로 만듦. Ollama 는 인증이 없으므로 보안 그룹에서 11434 를 백엔드만 허용해야 함.
# 사용: scp deploy/ai.sh ubuntu@<AI>:  →  ssh ubuntu@<AI> sudo sh ai.sh
set -e
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_MAX_LOADED_MODELS=2
Environment=OLLAMA_NUM_PARALLEL=1
EOF
systemctl daemon-reload
systemctl restart ollama
sleep 3
ollama pull qwen2.5:3b
