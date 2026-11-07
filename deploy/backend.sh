#!/bin/sh
# 백엔드 서버(EC2): Caddy(HTTPS) + PostgreSQL + Osori 백엔드(Spring Boot) + AI 서비스(web/server.py)
# 사용: tar czf - backend/build/libs/osori-0.0.1-SNAPSHOT.jar web deploy | ssh ubuntu@<백엔드> tar xzf -
#       ssh ubuntu@<백엔드> sudo AI_IP=<AI 사설 IP> DOMAIN=<도메인> sh deploy/backend.sh
# DB 비밀번호는 /etc/safety/backend.env 에 있어 다시 배포해도 유지됨. 위험구역 적재는 내용이 같으면 바꾸지 않음
set -e
: "${AI_IP:?AI 서버 사설 IP}" "${DOMAIN:?도메인}"
apt-get update -qq
apt-get install -y -qq caddy openjdk-21-jre-headless postgresql
systemctl disable --now mariadb 2>/dev/null || true # 이전 백엔드의 DB. 데이터는 지우지 않음
systemctl stop safety-backend safety-web 2>/dev/null || true

for dir in /etc/postgresql/*/main/conf.d; do
  printf 'shared_buffers = 64MB\nmax_connections = 20\n' > "$dir/60-safety.conf"
done
systemctl restart postgresql
install -d -m 750 /etc/safety
if ! grep -q postgresql /etc/safety/backend.env 2>/dev/null; then
  password=$(openssl rand -hex 16)
  sudo -u postgres psql -qc "CREATE USER osori PASSWORD '$password'"
  sudo -u postgres createdb -O osori osori
  (umask 077; cat > /etc/safety/backend.env <<ENV
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/osori
SPRING_DATASOURCE_USERNAME=osori
SPRING_DATASOURCE_PASSWORD=$password
SPRING_JPA_HIBERNATE_DDL_AUTO=update
SERVER_ADDRESS=127.0.0.1
SERVER_PORT=8080
ENV
)
fi

rm -rf /opt/safety
install -d /opt/safety/web
cp web/server.py web/index.html web/accident_data.json web/sample_data.json /opt/safety/web/
cp deploy/seed_backend.py deploy/risk-zones.json /opt/safety/
cp backend/build/libs/osori-0.0.1-SNAPSHOT.jar /opt/safety/backend.jar

JAVA="/usr/bin/java -Xmx300m -Xss512k -XX:+UseSerialGC -XX:TieredStopAtLevel=1 -jar /opt/safety/backend.jar"
# 공공데이터 위험구역 적재 (백엔드 적재 기능: 스키마 생성 후 끝나고 종료)
(set -a; . /etc/safety/backend.env; set +a; $JAVA --spring.main.web-application-type=none --risk-zone-import.file=/opt/safety/risk-zones.json | grep -E "적재|ERROR" || true)

cat > /etc/systemd/system/safety-backend.service <<UNIT
[Unit]
Description=Osori backend (Spring Boot)
After=network-online.target postgresql.service
Requires=postgresql.service

[Service]
ExecStart=$JAVA
EnvironmentFile=/etc/safety/backend.env
Environment=TZ=Asia/Seoul
DynamicUser=yes
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/safety-web.service <<UNIT
[Unit]
Description=Safety AI service (demo site, chat)
After=network-online.target safety-backend.service

[Service]
ExecStart=/usr/bin/python3 /opt/safety/web/server.py
Environment=CHAT_URL=http://$AI_IP:11434 CHAT_MODEL=safety-chat
Environment=BACKEND_URL=http://127.0.0.1:8080
Environment=TZ=Asia/Seoul
DynamicUser=yes
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	reverse_proxy /api/v1/* localhost:8080
	reverse_proxy localhost:8787
}
CADDY
systemctl daemon-reload
systemctl enable safety-backend safety-web
systemctl restart safety-backend safety-web caddy

# 빈 DB 면 시연 데이터를 넣음 (이미 있으면 그대로)
until curl -sf -o /dev/null "http://127.0.0.1:8080/api/v1/proximity-events?size=1"; do sleep 2; done
TZ=Asia/Seoul python3 /opt/safety/seed_backend.py /opt/safety/web/sample_data.json || true
