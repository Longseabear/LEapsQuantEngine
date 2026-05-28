# Sleeve Section Map

This map resolves LEapsQuantEngine sleeve names and manager roles to Codex
multi-agent targets.
The operator may call the target a `sectionID`; `multi_agent_v1.send_input`
uses the same value as `target`.

Missing values intentionally use the sentinel `unkown`.

## [Sleeve]

| sleeve_id | agent_profile | workspace_path | section_id | notes |
| --- | --- | --- | --- | --- |
| LEaps | Sleeve-LEaps | sleeves/LEaps | 019e0f7a-0ef1-7653-becc-0c9e79f29071 | Verified by operator. |
| us_etf_rotation | Sleeve-us_etf_rotation | sleeves/us_etf_rotation | 019e1231-a998-7191-bd12-57b300174ad4 | Verified by operator. |
| semiconduct-kor | Sleeve-semiconduct-kor | sleeves/semiconduct-kor | 019e4f6d-a795-7a61-b8db-600811ee9115 | Verified by operator. |
| kr-domestic-4401 | Sleeve-kr-domestic-4401 | sleeves/kr-domestic-4401 | 019e4da7-1cd0-7061-a77c-d0f60a0614ba | Verified by operator. Alias: kr-core-compass. |
| kr-lowvol-defensive | unkown | sleeves/kr-lowvol-defensive | 019e31f9-d12b-7c31-8bb6-8b71b839dedf | Verified by operator. Alias: leaps-lowvol / leaps lowvol. |
| default sleeve | unkown | sleeves/default sleeve | unkown | Default/sample sleeve workspace; not treated as operational unless the operator says so. |

## [Manager]

| manager_id | role | section_id | notes |
| --- | --- | --- | --- |
| news-daily-collector | 뉴스/일봉 수집가 | 019e4ed0-95ef-78c3-b393-56c96069e0d7 | Verified by operator. Collects news and daily bars. |
