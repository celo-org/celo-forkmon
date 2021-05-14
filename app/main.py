import datetime
import web3

from web3 import Web3, HTTPProvider
from web3.middleware import geth_poa_middleware
from flask import Flask, request, make_response, send_file
import heapq
import json
import logging
import hexbytes
from chainstate import config

graph_length = 16
block_interval_average_len = 500
cache_duration = 3600
cache_blocks = 1000
app = Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True

def get_nodes():
    if app.debug:
        return config.nodes_debug
    else:
        return config.nodes_production

def hash_of(blockOrHash):
    retval = blockOrHash
    if type(blockOrHash) == web3.datastructures.AttributeDict:
        blockOrHash = blockOrHash['hash']
    if type(blockOrHash) == dict:
        blockOrHash = blockOrHash['hash']
    if type(blockOrHash) == hexbytes.HexBytes:
        retval = blockOrHash.hex()
    else:
        retval = blockOrHash
    return retval

def to_dict(block):
    """ Blocks can be quite large, so we use this method to strip it down and only
    retain the bare essentials in memory, also converting hashes (ByteArray) into
    regular hex-strings
    """
    blockCountDown = 6774000 - block['number']
    parentHash = hash_of(block['parentHash'])
    return {
        'number': block['number'],
        'timestamp': block['timestamp'],
        'hash': hash_of(block),
        'size': block['size'],
        'parentHash' : parentHash,
        'parents': [parentHash],
        'blockCountDown': blockCountDown
    }

clients = {}
def get_client(name):
    if len(clients) == 0:
        clients.update({name: Web3(Web3.HTTPProvider(node['url'])) for name, node in get_nodes().items()})
    return clients[name]

class BlockFetcher(object):
    def __init__(self, client, cache_duration=3600, cache_blocks=1000):
        self.cache_duration = cache_duration
        self.cache_blocks = cache_blocks
        self.client = client
        self.client.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.block_hash_heap = []
        self.block_hash_cache = {}
        self.block_number_cache = {}
        self.latest = 0

    def get_latest(self):
        block = to_dict(self.client.eth.getBlock('latest'))
        h = block['hash']
        if h not in self.block_hash_cache and block is not None:
            self.block_hash_cache[h] = block
            self.block_number_cache[block['number']] = block
            ts = block['timestamp']
            self.latest = max(self.latest, ts)
            self.tidy_heap()
            heapq.heappush(self.block_hash_heap, (ts, h, block['number']))
        return block

    def get_block_by_hash(self, h):
        h = hash_of(h)
        if h not in self.block_hash_cache:
            app.logger.debug("Fetching block not in cache: %s", h)
            block = to_dict(self.client.eth.getBlock(h))
            self.block_hash_cache[h] = block
            if block is not None:
                self.block_number_cache[block['number']] = block
                ts = block['timestamp']
                self.latest = max(self.latest, ts)
                self.tidy_heap()
                heapq.heappush(self.block_hash_heap, (ts, h, block['number']))
        return self.block_hash_cache[h]

    def get_block_by_number(self, num):
        if num not in self.block_number_cache:
            app.logger.debug("Fetching block not in cache: %d", num)
            block = to_dict(self.client.eth.getBlock(int(num)))
            self.block_number_cache[num] = block
            if block is not None:
                h = hash_of(block)
                self.block_hash_cache[h] = block
                ts = block['timestamp']
                self.latest = max(self.latest, ts)

                self.tidy_heap()
                heapq.heappush(self.block_hash_heap, (ts, h, num))
        return self.block_number_cache[num]

    def tidy_heap(self):
        while len(self.block_hash_heap) > self.cache_blocks and self.block_hash_heap[0][0] < self.latest - self.cache_duration:
            blockts, blockhash, blocknum = heapq.heappop(self.block_hash_heap)
            del self.block_number_cache[blocknum]
            del self.block_hash_cache[blockhash]

fetchers = {}
def get_fetcher(name):
    if len(fetchers) == 0:
        fetchers.update({name: BlockFetcher(get_client(name), cache_duration, cache_blocks) for name in get_nodes()})
    return fetchers[name]

def find_ancestors(roots, earliest):
    blocks = {}
    for clientname, roothash in roots:
        frontier = set([roothash])
        while frontier:
            blockhash = frontier.pop()
            block = get_fetcher(clientname).get_block_by_hash(blockhash)
            if block is None:
                app.logger.debug("Discarded missing block with hash %s", blockhash)
                continue
            blocks[hash_of(block)] = block
            ts = block['timestamp']
            if ts >= earliest:
                if hash_of(block['parentHash']) not in blocks:
                    frontier.add(block['parentHash'])
    return blocks

def build_block_graph(roots, earliest):
    blocks = find_ancestors(roots, earliest)
    nodes = [block for (h,block) in blocks.items()]
    nodes.sort(key=lambda node: node['number'])
    return nodes

lastpolled = {}
latest_blocks = {}
def get_latest_block(clientname):
    if clientname not in lastpolled or datetime.datetime.now() - lastpolled[clientname] > datetime.timedelta(seconds=5):
        latest_blocks[clientname] = get_fetcher(clientname).get_latest()
    return latest_blocks[clientname]

def build_block_info(clientname):
    latest = get_latest_block(clientname)
    latestNumber = int(latest['number'])
    latestTimestamp = latest['timestamp']
    blockCountDown = 6774000 - latestNumber
    blockInterval = 0
    diffStyle = "text-muted"
    tdStyle = "text-muted"
    intervalStyle = "text-muted"
    earlierNumber = latestNumber - block_interval_average_len
    if earlierNumber > 0:
        try:
            earlier = get_fetcher(clientname).get_block_by_number(latestNumber - block_interval_average_len)
        except Exception as e:
            return None
        earlierTimestamp = earlier['timestamp']

        blockInterval = (latestTimestamp - earlierTimestamp) / float(block_interval_average_len)
    if blockInterval <= 15 and blockInterval > 0:
        intervalStyle = "text-success"
    elif blockInterval <= 30 and blockInterval > 0:
        intervalStyle = "text-warning"
    else:
        intervalStyle = "text-danger"
    clientversion = clientname.split(',')[2]
    clientfork = clientname.split(',')[1]
    forkStyle = "text-muted"
    cleanname = clientname.split(',')[0]
    if clientfork == 'Phoenix':
        forkStyle = 'text-success'
    elif clientfork == 'Aztlan':
        forkStyle = 'text-warning'
    elif clientfork == 'Agharta':
        forkStyle = 'text-danger'
    elif clientfork == 'Atlantis':
        forkStyle = 'text-danger'
    else:
        if len(clientfork) <= 3:
            clientfork = 'Unknown'
        forkStyle = 'text-danger'
    return {
        'number': latestNumber,
        'timestamp': latestTimestamp,
        'hash': latest['hash'],
        'shortHash': latest['hash'][:10],
        'blockInterval': "%.1f" % (blockInterval,),
        'name': clientname,
        'clean': cleanname,
        'fork': clientfork,
        'version': clientversion,
        'forkStyle': forkStyle,
        'explore': get_nodes()[clientname]['explorer'] % (latest['number'],),
        'diffStyle': diffStyle,
        'tdStyle': tdStyle,
        'intervalStyle': intervalStyle,
        'blockCountDown': blockCountDown,
    }

def build_block_infos():
    infos = [build_block_info(name) for name in get_nodes()]
    infos = [x for x in infos if x != None]
    return infos

@app.route('/')
def index():
    return send_file('./static/index.html')

@app.route('/favicon.ico')
def favicon():
    return send_file('./static/favicon.ico')

@app.route('/blocks')
def blocks():
    blockinfos = build_block_infos()
    latest = max(block['timestamp'] for block in blockinfos)
    earliest = max(int(request.args.get('since', latest - 300)), latest - cache_duration)
    roots = [(block['name'], block['hash']) for block in blockinfos]
    nodes = build_block_graph(roots, earliest)
    response = make_response(json.dumps({
        'latest': blockinfos,
        'nodes': nodes,
    }, indent=4))
    response.headers['Content-Type'] = 'text/json'
    return response

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
