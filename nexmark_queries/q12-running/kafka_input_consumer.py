import sys
from aiokafka import AIOKafkaConsumer
import pandas as pd

import asyncio
import uvloop
from universalis.common.serialization import msgpack_deserialization
from universalis.common.networking import NetworkingManager

protocol = sys.argv[1]
networking = NetworkingManager()

async def consume():
    records = []
    consumer = AIOKafkaConsumer(
        'bidsSource',
        key_deserializer=msgpack_deserialization,
        bootstrap_servers='localhost:9093',
        auto_offset_reset="earliest")
    await consumer.start()
    try:
        # Consume messages
        async for msg in consumer:
            value = networking.decode_message(msg.value)
            print("consumed: ", msg.key, value, msg.timestamp)
            records.append((msg.key, value, msg.timestamp))
    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()
        pd.DataFrame.from_records(records, columns=['request_id', 'request', 'timestamp']).to_csv(f'./results/q12r/{protocol}-input.csv',
                                                                                                   index=False)

uvloop.install()


asyncio.run(consume())