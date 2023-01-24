import asyncio
import io
import os
import time
from timeit import default_timer as timer
from math import ceil

import aiozmq
import uvloop
import zmq
from aiokafka import AIOKafkaConsumer, TopicPartition, AIOKafkaProducer
from aiokafka.errors import UnknownTopicOrPartitionError, KafkaConnectionError
from minio import Minio
from miniopy_async import Minio as Minio_async

from universalis.common.local_state_backends import LocalStateBackend
from universalis.common.logging import logging
from universalis.common.networking import NetworkingManager
from universalis.common.operator import Operator
from universalis.common.serialization import Serializer, msgpack_serialization, compressed_msgpack_serialization, compressed_msgpack_deserialization
from worker.operator_state.in_memory_state import InMemoryOperatorState
from worker.operator_state.redis_state import RedisOperatorState
from worker.operator_state.stateless import Stateless
from worker.run_func_payload import RunFuncPayload

SERVER_PORT: int = 8888
DISCOVERY_HOST: str = os.environ['DISCOVERY_HOST']
DISCOVERY_PORT: int = int(os.environ['DISCOVERY_PORT'])
KAFKA_URL: str = os.getenv('KAFKA_URL', None)
INGRESS_TYPE = os.getenv('INGRESS_TYPE', None)
EGRESS_TOPIC_NAME: str = 'universalis-egress'

MINIO_URL: str = f"{os.environ['MINIO_HOST']}:{os.environ['MINIO_PORT']}"
MINIO_ACCESS_KEY: str = os.environ['MINIO_ROOT_USER']
MINIO_SECRET_KEY: str = os.environ['MINIO_ROOT_PASSWORD']
SNAPSHOT_BUCKET_NAME: str = "universalis-snapshots"


class Worker:

    def __init__(self):
        self.id: int = -1
        self.networking = NetworkingManager()
        self.router = None
        self.kafka_egress_producer = None
        self.operator_state_backend = None
        self.registered_operators: dict[tuple[str, int], Operator] = {}
        self.dns: dict[str, dict[str, tuple[str, int]]] = {}
        self.topic_partitions: list[TopicPartition] = []
        self.peers: dict[int, tuple[str, int]] = {}  # worker_id: (host, port)
        self.local_state: InMemoryOperatorState | RedisOperatorState | Stateless = Stateless()
        # background task references
        self.background_tasks = set()
        self.last_messages_processed = {}
        # snapshot
        self.minio_client: Minio = Minio(
            MINIO_URL, access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY, secure=False
        )
        self.minio_client_async: Minio_async = Minio_async(
            MINIO_URL, access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY, secure=False
        )
        self.snapshot_state_lock: asyncio.Lock = asyncio.Lock()
        self.last_snapshot_timestamp = time.time_ns() // 1000000
        self.total_partitions_per_operator = {}

    async def run_function(
            self,
            payload: RunFuncPayload,
            send_from = None
    ) -> bool:
        success: bool = True
        operator_partition = self.registered_operators[(payload.operator_name, payload.partition)]
        response = await operator_partition.run_function(
            payload.key,
            payload.request_id,
            payload.timestamp,
            payload.function_name,
            payload.params
        )
        # If exception we need to add it to the application logic aborts
        if isinstance(response, Exception):
            success = False
        # If request response send the response
        if payload.response_socket is not None:
            self.router.write(
                (payload.response_socket, self.networking.encode_message(
                    response,
                    Serializer.MSGPACK
                ))
            )
        # If we have a response, and it's not part of the chain send it to kafka
        elif response is not None:
            # logging.warning(f'payload: {payload.operator_name}')
            # If Exception transform it to string for Kafka
            if isinstance(response, Exception):
                kafka_response = str(response)
            else:
                kafka_response = response
            # If fallback add it to the fallback replies else to the response buffer
            self.create_task(self.kafka_egress_producer.send_and_wait(
                EGRESS_TOPIC_NAME,
                key=payload.request_id,
                value=msgpack_serialization(kafka_response)
            ))
        if send_from is not None:
            incoming_channel = send_from['operator_name'] +'_'+ payload.operator_name +'_'+ str(send_from['operator_partition']*(self.total_partitions_per_operator[payload.operator_name]) + payload.partition)
            self.last_messages_processed[incoming_channel] = send_from['kafka_offset']
        return success

    # if you want to use this run it with self.create_task(self.take_snapshot())
    async def take_snapshot(self):
        snap_start = timer()
        # logging.warning(self.last_messages_processed)
        if isinstance(self.local_state, InMemoryOperatorState):
            self.local_state: InMemoryOperatorState
            async with self.snapshot_state_lock:
                # Flush the current kafka message buffer from networking to make sure the messages are in Kafka.
                last_messages_sent = await self.networking.flush_kafka_buffer()
                snapshot_data = {}
                snapshot_data['last_messages_processed'] = self.last_messages_processed
                self.last_messages_processed = {}
                snapshot_data['local_state_data'] = self.local_state.data
                bytes_file: bytes = compressed_msgpack_serialization(snapshot_data)
            snapshot_time = time.time_ns() // 1000000
            snapshot_name: str = f"snapshot_{self.id}_{snapshot_time}.bin"
            await self.minio_client_async.put_object(
                bucket_name=SNAPSHOT_BUCKET_NAME,
                object_name=snapshot_name,
                data=io.BytesIO(bytes_file),
                length=len(bytes_file)
            )
            self.last_snapshot_timestamp = time.time_ns() // 1000000
            coordinator_info = {}
            coordinator_info['last_messages_processed'] = snapshot_data['last_messages_processed']
            coordinator_info['last_messages_sent'] = last_messages_sent
            coordinator_info['snapshot_name'] = snapshot_name
            await self.networking.send_message(
                DISCOVERY_HOST, DISCOVERY_PORT,
                {
                    "__COM_TYPE__": 'SNAPSHOT_TAKEN',
                    "__MSG__": coordinator_info
                },
                Serializer.MSGPACK
            )
        else:
            logging.warning("Snapshot currently supported only for in-memory operator state")
        snap_end = timer()
        logging.warning(f"Snapshot took: {snap_end - snap_start}, taken at {time.time_ns() // 1000000}")

    async def restore_from_snapshot(self, snapshot_to_restore):
        # If timestamp is zero, it means reset from the beginning
        # Therefore we reset the local state to an empty dict
        if snapshot_to_restore == 0:
            self.local_state.data = {}
        else:
            state_to_restore = self.minio_client.get_object(
                bucket_name=SNAPSHOT_BUCKET_NAME,
                object_name=snapshot_to_restore
            ).data
            async with self.snapshot_state_lock:
                self.local_state.data = compressed_msgpack_deserialization(state_to_restore)
        logging.warning(f"Snapshot restored to: {snapshot_to_restore}")


    async def start_kafka_egress_producer(self):
        self.kafka_egress_producer = AIOKafkaProducer(
            bootstrap_servers=[KAFKA_URL],
            enable_idempotence=True,
            compression_type="gzip"
        )
        while True:
            try:
                await self.kafka_egress_producer.start()
            except KafkaConnectionError:
                time.sleep(1)
                logging.info("Waiting for Kafka")
                continue
            break

    async def start_kafka_consumer(self, topic_partitions: list[TopicPartition]):
        logging.info(f'Creating Kafka consumer for topic partitions: {topic_partitions}')
        consumer = AIOKafkaConsumer(bootstrap_servers=[KAFKA_URL])
        consumer.assign(topic_partitions)
        while True:
            # start the kafka consumer
            try:
                await consumer.start()
            except (UnknownTopicOrPartitionError, KafkaConnectionError):
                time.sleep(1)
                logging.warning(f'Kafka at {KAFKA_URL} not ready yet, sleeping for 1 second')
                continue
            break
        try:
            # Consume messages
            while True:
                result = await consumer.getmany(timeout_ms=1)
                for _, messages in result.items():
                    if messages:
                        for message in messages:
                            self.handle_message_from_kafka(message)
        finally:
            await consumer.stop()

    async def replay_from_kafka(self, channel, offset):
        sent_op, rec_op, partition = channel.split('_')
        # Create a kafka consumer for the given channel and seek the given offset.
        # For every kafka message, send over TCP without logging the message sent.
        consumer = AIOKafkaConsumer(bootstrap_servers=[KAFKA_URL])
        topic_partition = TopicPartition(sent_op+rec_op, int(partition))
        consumer.assign([topic_partition])
        while True:
            # start the kafka consumer
            try:
                consumer.seek(topic_partition, offset)
                await consumer.start()
            except (UnknownTopicOrPartitionError, KafkaConnectionError):
                time.sleep(1)
                logging.warning(f'Kafka at {KAFKA_URL} not ready yet, sleeping for 1 second')
                continue
            break
        try:
            # Consume messages
            while True:
                result = await consumer.getmany(timeout_ms=1)
                for _, messages in result.items():
                    if messages:
                        for message in messages:
                            await self.replay_log_message(message)
        finally:
            await consumer.stop()        

    async def replay_log_message(self, msg):
        deserialized_data: dict = self.networking.decode_message(msg.value)
        receiver_info = deserialized_data['__MSG__']['__SENT_TO__']
        await self.networking.replay_message(receiver_info['host'], receiver_info['port'], msg)

    def handle_message_from_kafka(self, msg):
        logging.info(
            f"Consumed: {msg.topic} {msg.partition} {msg.offset} "
            f"{msg.key} {msg.value} {msg.timestamp}"
        )
        deserialized_data: dict = self.networking.decode_message(msg.value)
        # This data should be added to a replay kafka topic.
        message_type: str = deserialized_data['__COM_TYPE__']
        message = deserialized_data['__MSG__']
        # logging.warning(f"message key looks like: {msg.key}")
        if message_type == 'RUN_FUN':
            run_func_payload: RunFuncPayload = self.unpack_run_payload(message, msg.key, timestamp=msg.timestamp)
            logging.info(f'RUNNING FUNCTION FROM KAFKA: {run_func_payload.function_name} {run_func_payload.key}')
            # logging.warning(f'payload from kafka: {run_func_payload.response_socket}')
            self.create_task(
                self.run_function(
                    run_func_payload
                )
            )
        else:
            logging.error(f"Invalid message type: {message_type} passed to KAFKA")

    def create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def worker_controller(self, deserialized_data, resp_adr):
        message_type: str = deserialized_data['__COM_TYPE__']
        message = deserialized_data['__MSG__']
        match message_type:
            case 'RUN_FUN_REMOTE' | 'RUN_FUN_RQ_RS_REMOTE':
                request_id = message['__RQ_ID__']
                if message_type == 'RUN_FUN_REMOTE':
                    logging.info('CALLED RUN FUN FROM PEER')
                    sender_details = message['__SENT_FROM__']
                    payload = self.unpack_run_payload(message, request_id)
                    self.create_task(
                        self.run_function(
                            payload, send_from = sender_details
                        )
                    )
                else:
                    logging.info('CALLED RUN FUN RQ RS FROM PEER')
                    payload = self.unpack_run_payload(message, request_id, response_socket=resp_adr)
                    self.create_task(
                        self.run_function(
                            payload
                        )
                    )
            case 'RECOVER_FROM_SNAPSHOT':
                logging.warning(f'Recovery message received: {message}')
                # Build the snapshot name from the recovery message received
                snapshot_to_restore = f'snapshot_{self.id}_{message[0]}.bin'
                await self.restore_from_snapshot(snapshot_to_restore)
                # Replay channels from corresponding offsets in message[1]
                for channel in message[1].keys():
                    await self.replay_from_kafka(channel, message[1][channel])
            case 'RECEIVE_EXE_PLN':  # RECEIVE EXECUTION PLAN OF A DATAFLOW GRAPH
                # This contains all the operators of a job assigned to this worker

                # Message that tells the worker its execution plan from round_robin.schedule

                await self.handle_execution_plan(message)
                self.attach_state_to_operators()
            # ADD CASE FOR TESTING SNAPSHOT RESTORE
            case _:
                logging.error(f"Worker Service: Non supported command message type: {message_type}")

    def attach_state_to_operators(self):
        operator_names: set[str] = set([operator.name for operator in self.registered_operators.values()])
        if self.operator_state_backend == LocalStateBackend.DICT:
            self.local_state = InMemoryOperatorState(operator_names)
        elif self.operator_state_backend == LocalStateBackend.REDIS:
            self.local_state = RedisOperatorState(operator_names)
        else:
            logging.error(f"Invalid operator state backend type: {self.operator_state_backend}")
            return
        for operator in self.registered_operators.values():
            operator.attach_state_networking(self.local_state, self.networking, self.dns)

    async def handle_execution_plan(self, message):
        worker_operators, self.dns, self.peers, self.operator_state_backend, self.total_partitions_per_operator = message
        await self.networking.set_total_partitions_per_operator(self.total_partitions_per_operator)
        del self.peers[self.id]
        operator: Operator
        for tup in worker_operators:
            operator, partition = tup
            self.registered_operators[(operator.name, partition)] = operator
            if INGRESS_TYPE == 'KAFKA':
                self.topic_partitions.append(TopicPartition(operator.name, partition))
        await self.networking.start_kafka_producer()
        self.create_task(self.start_kafka_consumer(self.topic_partitions))
        logging.info(
            f'Registered operators: {self.registered_operators} \n'
            f'Peers: {self.peers} \n'
            f'Operator locations: {self.dns}'
        )

    async def uncoordinated_checkpointing(self, checkpoint_interval):
        while True:
            await asyncio.sleep(checkpoint_interval)
            await self.take_snapshot()

    async def communication_induced_checkpointing(self, checkpoint_interval):
        while True:
            asyncio.sleep(checkpoint_interval)
            current_time = time.time_ns // 1000000
            if current_time > self.last_snapshot_timestamp + checkpoint_interval*1000:
                await self.take_snapshot()
            else:
                asyncio.sleep(ceil((self.last_snapshot_timestamp + checkpoint_interval*1000 - current_time) / 1000))
            

    async def start_tcp_service(self):
        self.router = await aiozmq.create_zmq_stream(zmq.ROUTER, bind=f"tcp://0.0.0.0:{SERVER_PORT}")
        await self.start_kafka_egress_producer()
        logging.info(
            f"Worker TCP Server listening at 0.0.0.0:{SERVER_PORT} "
            f"IP:{self.networking.host_name}"
        )
        self.create_task(self.uncoordinated_checkpointing(5))
        while True:
            # This is where we read from TCP, log at receiver
            resp_adr, data = await self.router.read()
            deserialized_data: dict = self.networking.decode_message(data)
            if '__COM_TYPE__' not in deserialized_data:
                logging.error(f"Deserialized data do not contain a message type")
            else:
                await self.worker_controller(deserialized_data, resp_adr)

    @staticmethod
    def unpack_run_payload(
            message: dict, request_id: bytes,
            timestamp=None, response_socket=None
    ) -> RunFuncPayload:
        timestamp = message['__TIMESTAMP__'] if timestamp is None else timestamp
        return RunFuncPayload(
            request_id, message['__KEY__'], timestamp,
            message['__OP_NAME__'], message['__PARTITION__'],
            message['__FUN_NAME__'], tuple(message['__PARAMS__']), response_socket
        )

    async def register_to_coordinator(self):
        self.id = await self.networking.send_message_request_response(
            DISCOVERY_HOST, DISCOVERY_PORT,
            {
                "__COM_TYPE__": 'REGISTER_WORKER',
                "__MSG__": self.networking.host_name
            },
            Serializer.MSGPACK
        )
        logging.info(f"Worker id: {self.id}")

    async def main(self):
        await self.register_to_coordinator()
        await self.start_tcp_service()


if __name__ == "__main__":
    uvloop.install()
    worker = Worker()
    asyncio.run(Worker().main())
