---
name: pipeline-redundancy-audit
description: >
  审计前端筛选条件与后端数据计算链路之间的不一致，
  识别并报告因前端已剔除但后端仍在全量计算的冗余数据链路。
  触发场景：当数据管道涉及“全量枚举 → 前端筛选”模式时，
  或用户提及“冗余计算”“全量跑但只用部分”“前端剔除后后端仍在跑”时激活。
allowed-tools: "Read, Glob, Grep, Bash"
---