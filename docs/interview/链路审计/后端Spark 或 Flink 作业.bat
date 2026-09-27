# 在全量模式下捕获执行计划
spark-submit --explain ... job.py > plan_full.txt

# 在前端筛选后的板块列表下执行
spark-submit --explain ... job.py --sectors=140_concepts > plan_filtered.txt

# 对比执行计划中 Scan 节点的输入行数和 Filter 节点的谓词
diff <(grep "Scan\|Filter\|Join" plan_full.txt) \
     <(grep "Scan\|Filter\|Join" plan_filtered.txt)