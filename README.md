# Osori-Presentation

저소음 차량 보행자 촉각 경고의 시연 사이트와 AI 서비스입니다.

- `web/index.html`: 프레젠테이션 사이트 (↑↓ 이동, F 전체 화면). 사고다발 지도, 실시간 시연, AI 채팅과 통신 콘솔, 내비게이션 예시
- `web/server.py`: 사이트 제공, 동시 다발 구역 계산, 채팅. 질문마다 백엔드를 읽어 답을 만들고 AI 가 쓰듯 조금씩 보냄
- `web/answers.py`: 채팅 답. 질문 유형을 가려 본문(요약·표·가이드)은 코드가 쓰고, 끝의 **AI 분석** 1~2문장만 모델이 씀. 거리·RSSI 해석과 권장 행동도 여기서 계산
- `deploy/`: 서버 설치(`backend.sh`, `ai.sh`), 공공데이터 변환(`make_zones.py`), 시연 이벤트 적재(`seed_backend.py`)

## 함께 쓰는 저장소

| 저장소 | 역할 |
| --- | --- |
| [Osori-BE](https://github.com/BMW-Osori/Osori-BE) | 백엔드 (Spring Boot, PostgreSQL) |
| [Osori-QWEN](https://github.com/BMW-Osori/Osori-QWEN) | AI 분석 모델 튜닝 (Qwen2.5-3B) |
| [Osori-FE](https://github.com/BMW-Osori/Osori-FE) | 앱 (Flutter) |

## 실행

```sh
BACKEND_URL=http://127.0.0.1:8080 CHAT_URL=http://<Ollama>:11434 CHAT_MODEL=safety-chat python3 web/server.py
python3 web/test_server.py   # 가짜 백엔드·모델로 검증
```

`BACKEND_URL` 이 없으면 `web/sample_data.json` 을 씁니다.

## 채팅 답이 만들어지는 방식

AI 서버(t4g.large, ARM 2코어)는 생성이 초당 약 10토큰이라, 모델이 긴 답을 다 쓰면 1분 가까이 걸립니다. 그래서 나눠 씁니다.

1. `answers.route` 가 질문 유형(구역·내 주변·지역·최근 감지·동시 다발·시간대·순위·요약·신호 수치·안내문·데이터 없음·도움말)을 가려 본문을 바로 만듭니다 (약 10ms)
2. 본문을 보내는 동안 짧은 데이터(해석·권장 행동을 붙인 몇 줄)로 모델에 **AI 분석**을 요청합니다. 모델이 데이터를 읽는 시간이 본문 전송과 겹칩니다
3. 본문 → AI 분석 → 출처 순서로 AI 가 쓰듯 보냅니다 (토큰처럼 1~3자씩, 초당 약 70자, 본문은 최대 7초)
4. 모델 서버가 꺼졌거나 실패하면 코드가 쓴 기준 분석으로 대신합니다. 안내문·데이터 없음·도움말은 모델을 부르지 않습니다

모델에 주는 데이터 형식(`answers.py` 의 각 `*_reply` 의 `data`)을 바꾸면 [Osori-QWEN](https://github.com/BMW-Osori/Osori-QWEN) 에서 다시 학습해야 합니다.

## 배포

Osori-BE 에 인증 설정이 없어 그대로 띄우면 Spring Security 기본값이 모든 API 를 막습니다. `deploy/backend-patch/SecurityConfig.java` 를 `src/main/java/com/bmw/osori/global/config/` 에 넣고 빌드하세요 (전부 허용: 공개 서비스에는 인증을 따로 설계해야 합니다).

```sh
git clone https://github.com/BMW-Osori/Osori-BE backend    # 이 저장소 안에 backend 로
cd backend && ./gradlew test bootJar && cd ..              # JDK 21
COPYFILE_DISABLE=1 tar czf - --no-xattrs backend/build/libs/osori-0.0.1-SNAPSHOT.jar web/server.py web/answers.py web/index.html web/accident_data.json web/sample_data.json deploy \
  | ssh ubuntu@<백엔드> 'rm -rf web deploy backend; tar xzf -'
ssh ubuntu@<백엔드> 'sudo AI_IP=<AI 사설 IP> DOMAIN=<도메인> sh deploy/backend.sh'
```

## 데이터

- 위험구역: 한국도로교통공단 [전국교통사고다발지역표준데이터](https://www.data.go.kr/data/15029185/standard.do) 최근 3년(2022–2024) 1,552곳. 사망 12·중상 6·경상 3·부상신고 1 로 환산해 상위 1% 값을 100점으로, HIGH 70점·MEDIUM 40점 이상
- 접근 이벤트: 시연용 가상 데이터 (`web/sample_data.json`, 사이트의 시뮬레이션 버튼)

CSV 를 새로 받으면 `python3 deploy/make_zones.py <csv>`.

지도 © OpenStreetMap contributors (대량·오프라인 타일 금지), 글꼴 Pretendard(SIL OFL). AI 분석 모델 표기는 `NOTICE` 참고.
