import asyncio
import random
import subprocess
import time
import pandas as pd
from timeit import default_timer as timer

import uvloop
from universalis.common.stateflow_ingress import IngressTypes
from universalis.universalis import Universalis
from universalis.common.logging import logging

from operators import q3_graph

UNIVERSALIS_HOST: str = 'localhost'
UNIVERSALIS_PORT: int = 8886
KAFKA_URL = 'localhost:9093'


async def main():
    universalis = Universalis(UNIVERSALIS_HOST, UNIVERSALIS_PORT,
                              ingress_type=IngressTypes.KAFKA,
                              kafka_url=KAFKA_URL)
    await universalis.start()
    ####################################################################################################################
    # SUBMIT STATEFLOW GRAPH ###########################################################################################
    ####################################################################################################################
    await universalis.submit(q3_graph.g)

    print('Graph submitted')


    time.sleep(1)
    input("Press when you want to start producing.")

    subprocess.call(["java", "-jar", "nexmark/target/nexmark-generator-1.0-SNAPSHOT-jar-with-dependencies.jar",
               "--query", "3",
               "--generator-parallelism", "1",
               "--enable-auctions-topic", "true",
               "--enable-persons-topic", "true",
               "--load-pattern", "static",
               "--experiment-length", "1",
               "--use-default-configuration", "false",
               "--rate", "1000",
               "--max-noise", "0",
               "--iteration-duration-ms", "60000",
               "--kafka-server", "localhost:9093"
               ])

    await universalis.close()



uvloop.install()
asyncio.run(main())