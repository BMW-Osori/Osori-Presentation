package com.bmw.osori.global.config;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.web.SecurityFilterChain;

/** 명세에 인증이 없어 전부 허용. 이 빈이 없으면 Spring Security 기본값이 모든 API 를 막음. */
@Configuration
@ConditionalOnWebApplication // 위험구역 적재 실행(web-application-type=none)에는 HttpSecurity 가 없음
public class SecurityConfig {

	@Bean
	SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
		return http
			.csrf(csrf -> csrf.disable())
			.authorizeHttpRequests(auth -> auth.anyRequest().permitAll())
			.build();
	}
}
