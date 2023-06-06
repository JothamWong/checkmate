import pandas as pd
import numpy as np
import math
import matplotlib.pyplot as plt


input_msgs = pd.read_csv('./results/q1/input.csv')
output_msgs = pd.read_csv('./results/q1/output.csv')

joined = pd.merge(input_msgs, output_msgs, on='request_id', how='outer')
runtime = joined['timestamp_y'] - joined['timestamp_x']

joined_sorted = joined.sort_values('timestamp_x')

runtime_no_nan = runtime.dropna()
print(f'min latency: {min(runtime_no_nan)}ms')
print(f'max latency: {max(runtime_no_nan)}ms')
print(f'average latency: {np.average(runtime_no_nan)}ms')
print(f'99%: {np.percentile(runtime_no_nan, 99)}ms')
print(f'95%: {np.percentile(runtime_no_nan, 95)}ms')
print(f'90%: {np.percentile(runtime_no_nan, 90)}ms')
print(f'75%: {np.percentile(runtime_no_nan, 75)}ms')
print(f'60%: {np.percentile(runtime_no_nan, 60)}ms')
print(f'50%: {np.percentile(runtime_no_nan, 50)}ms')
print(f'25%: {np.percentile(runtime_no_nan, 25)}ms')
print(f'10%: {np.percentile(runtime_no_nan, 10)}ms')
print(np.argmax(runtime_no_nan))
print(np.argmin(runtime_no_nan))

missed = joined[joined['response'].isna()]

if len(missed) > 0:
    print('--------------------')
    print('\nMISSED MESSAGES!\n')
    print('--------------------')
    print(missed)
    print('--------------------')
else:
    print('\nNO MISSED MESSAGES!\n')


start_time = -math.inf
latency_buckets = {}
bucket_id = -1

granularity = 100  # 1 second (ms) (i.e. bucket size)

for idx, t in enumerate(joined_sorted['timestamp_x']):
    if t - start_time > granularity:
        bucket_id += 1
        start_time = t
        latency_buckets[bucket_id] = [joined_sorted['timestamp_y'][idx] - joined_sorted['timestamp_x'][idx]]
    else:
        latency_buckets[bucket_id].append(joined_sorted['timestamp_y'][idx] - joined_sorted['timestamp_x'][idx])

# print(latency_buckets)

latency_buckets_99: dict[int, float] = {k*100: np.percentile(v, 99) for k, v in latency_buckets.items() if v}
latency_buckets_50: dict[int, float] = {k*100: np.percentile(v, 50) for k, v in latency_buckets.items() if v}

# print(latency_buckets_99)
# print(latency_buckets_50)

_, ax = plt.subplots()
ax.plot(latency_buckets_99.keys(), latency_buckets_99.values(), linewidth=2.5, label='99p')
ax.plot(latency_buckets_50.keys(), latency_buckets_50.values(), linewidth=2.5, label='50p')
ax.set_xlabel('Time (ms)')
ax.set_ylabel('Latency (ms)')
ax.legend(bbox_to_anchor=(0.5, -0.2), loc="center", ncol=2)
ax.set_title("NexMark Q1 - Uncoordinated")
plt.tight_layout()
plt.show()