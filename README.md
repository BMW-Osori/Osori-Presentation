# Osori-Presentation

저소음 차량 보행자 촉각 경고의 시연 사이트와 AI 서비스입니다.

- `web/index.html`: 프레젠테이션 사이트 (↑↓ 이동, F 전체 화면). 사고다발 지도, 실시간 시연, AI 채팅과 통신 콘솔, 내비게이션 예시
- `web/server.py`: 사이트 제공, 동시 다발 구역 계산, 채팅. 질문마다 백엔드를 읽어 모델의 참고 데이터로 붙임
- `deploy/`: 서버 설치(`backend.sh`, `ai.sh`), 공공데이터 변환(`make_zones.py`), 시연 이벤트 적재(`seed_backend.py`)

## 함께 쓰는 저장소

| 저장소 | 역할 |
| --- | --- |
| [Osori-BE](https://github.com/BMW-Osori/Osori-BE) | 백엔드 (Spring Boot, PostgreSQL) |
| [Osori-QWEN](https://github.com/BMW-Osori/Osori-QWEN) | 채팅 모델 튜닝 (Qwen2.5-3B) |
| [Osori-FE](https://github.com/BMW-Osori/Osori-FE) | 앱 (Flutter) |

## 실행

```sh
BACKEND_URL=http://127.0.0.1:8080 CHAT_URL=http://<Ollama>:11434 CHAT_MODEL=safety-chat python3 web/server.py
python3 web/test_server.py   # 가짜 백엔드·모델로 검증
```

`BACKEND_URL` 이 없으면 `web/sample_data.json` 을 씁니다.

## 배포

Osori-BE 에 인증 설정이 없어 그대로 띄우면 Spring Security 기본값이 모든 API 를 막습니다. `deploy/backend-patch/SecurityConfig.java` 를 `src/main/java/com/bmw/osori/global/config/` 에 넣고 빌드하세요 (전부 허용: 공개 서비스에는 인증을 따로 설계해야 합니다).

```sh
git clone https://github.com/BMW-Osori/Osori-BE backend    # 이 저장소 안에 backend 로
cd backend && ./gradlew test bootJar && cd ..              # JDK 21
COPYFILE_DISABLE=1 tar czf - --no-xattrs backend/build/libs/osori-0.0.1-SNAPSHOT.jar web/server.py web/index.html web/accident_data.json web/sample_data.json deploy \
  | ssh ubuntu@<백엔드> 'rm -rf web deploy backend; tar xzf -'
ssh ubuntu@<백엔드> 'sudo AI_IP=<AI 사설 IP> DOMAIN=<도메인> sh deploy/backend.sh'
```

## 데이터

- 위험구역: 한국도로교통공단 [전국교통사고다발지역표준데이터](https://www.data.go.kr/data/15029185/standard.do) 최근 3년(2022–2024) 1,552곳. 사망 12·중상 6·경상 3·부상신고 1 로 환산해 상위 1% 값을 100점으로, HIGH 70점·MEDIUM 40점 이상
- 접근 이벤트: 시연용 가상 데이터 (`web/sample_data.json`, 사이트의 시뮬레이션 버튼)

CSV 를 새로 받으면 `python3 deploy/make_zones.py <csv>`.

지도 © OpenStreetMap contributors (대량·오프라인 타일 금지), 글꼴 Pretendard(SIL OFL). 채팅 모델 표기는 `NOTICE` 참고.
