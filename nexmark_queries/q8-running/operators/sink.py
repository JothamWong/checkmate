from universalis.common.operator import StatefulFunction, Operator
from universalis.common.logging import logging

sink_operator = Operator('sink')

@sink_operator.register
async def output(ctx: StatefulFunction, joined_tuples):
    return joined_tuples