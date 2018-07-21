# Copyright (c) 2016-2017, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

from aiorpcx import _version as aiorpcx_version

import electrumx
from electrumx.lib.server_base import ServerBase
from electrumx.lib.util import version_string
from electrumx.server.chain_state import ChainState
from electrumx.server.peers import PeerManager
from electrumx.server.session import SessionManager


class Notifications(object):
    # hashX notifications come from two sources: new blocks and
    # mempool refreshes.  The logic in daemon.py only gets new mempool
    # hashes after getting the latest height.
    #
    # A user with a pending transaction is notified after the block it
    # gets in is processed.  Block processing can take an extended
    # time, and any mempool refreshes during that time will not have
    # the transaction in the mempool any more, which would cause a
    # redundant notification.  To avoid this, mempool touches are not
    # notified whilst a block is being processed, but combined with
    # the block notification when it is made.  We do not pause mempool
    # processing

    def __init__(self):
        self._touched_mp = {}
        self._touched_bp = {}
        self._highest_block = 0

    async def _maybe_notify(self):
        tmp, tbp = self._touched_mp, self._touched_bp
        common = set(tmp).intersection(tbp)
        if common:
            height = max(common)
        elif tmp and max(tmp) == self._highest_block:
            height = self._highest_block
        else:
            # Either we are processing a block and waiting for it to
            # come in, or we have had no mempool update for the
            # current block
            return
        touched = tmp.pop(height)
        touched.update(tbp.pop(height, set()))
        for old in [h for h in tmp if h <= height]:
            del tmp[old]
        for old in [h for h in tbp if h <= height]:
            del tbp[old]
        await self.notify_sessions(touched, height)

    async def on_mempool(self, touched, height):
        self._touched_mp[height] = touched
        await self._maybe_notify()

    async def on_block(self, touched, height):
        self._touched_bp[height] = touched
        self._highest_block = height
        await self._maybe_notify()

    async def notify_sessions(self, touched, height):
        pass


class Controller(ServerBase):
    '''Manages server initialisation and stutdown.

    Servers are started once the mempool is synced after the block
    processor first catches up with the daemon.
    '''

    AIORPCX_MIN = (0, 5, 6)

    def __init__(self, env):
        '''Initialize everything that doesn't require the event loop.'''
        super().__init__(env)

        if aiorpcx_version < self.AIORPCX_MIN:
            raise RuntimeError('ElectrumX requires aiorpcX >= '
                               f'{version_string(self.AIORPCX_MIN)}')

        min_str, max_str = env.coin.SESSIONCLS.protocol_min_max_strings()
        self.logger.info(f'software version: {electrumx.version}')
        self.logger.info(f'aiorpcX version: {version_string(aiorpcx_version)}')
        self.logger.info(f'supported protocol versions: {min_str}-{max_str}')
        self.logger.info(f'event loop policy: {env.loop_policy}')
        self.logger.info(f'reorg limit is {env.reorg_limit:,d} blocks')

        notifications = Notifications()
        self.chain_state = ChainState(env, self.tasks, notifications)
        self.peer_mgr = PeerManager(env, self.tasks, self.chain_state)
        self.session_mgr = SessionManager(env, self.tasks, self.chain_state,
                                          self.peer_mgr, notifications,
                                          self.shutdown_event)

    async def start_servers(self):
        '''Start the RPC server and wait for the mempool to synchronize.  Then
        start the peer manager and serving external clients.
        '''
        self.session_mgr.start_rpc_server()
        await self.chain_state.wait_for_mempool()
        self.peer_mgr.start_peer_discovery()
        self.session_mgr.start_serving()

    async def shutdown(self):
        '''Perform the shutdown sequence.'''
        # Close servers and connections - main source of new task creation
        await self.session_mgr.shutdown()
        # Flush chain state to disk
        await self.chain_state.shutdown()
        # Cancel all tasks; this shuts down the peer manager and prefetcher
        await self.tasks.cancel_all(wait=True)
