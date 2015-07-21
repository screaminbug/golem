import logging
import random
import os
import time

from golem.network.transport.Tcp import Network, HostData, nodeInfoToHostInfos
from golem.network.GNRServer import GNRServer
from golem.network.NetAndFilesConnState import NetAndFilesConnState
from golem.resource.DirManager import DirManager
from golem.resource.ResourcesManager import DistributedResourceManager
from golem.resource.ResourceSession import ResourceSession
from golem.ranking.Ranking import RankingStats

logger = logging.getLogger(__name__)

##########################################################
class ResourceServer(GNRServer):
    ############################
    def __init__(self, configDesc, keysAuth, client, useIp6=False):
        self.client = client
        self.keysAuth = keysAuth
        self.resourcesToSend = []
        self.resourcesToGet = []
        self.resSendIt = 0
        self.peersIt = 0
        self.dirManager = DirManager(configDesc.rootPath, configDesc.clientUid)
        self.resourceManager = DistributedResourceManager(self.dirManager.getResourceDir())
        self.useIp6=useIp6
        GNRServer.__init__(self, configDesc, None, ResourceSessionFactory(), useIp6)

        self.resourcePeers = {}
        self.waitingTasks = {}
        self.waitingTasksToCompute = {}
        self.waitingResources = {}

        self.lastGetResourcePeersTime  = time.time()
        self.getResourcePeersInterval = 5.0
        self.sessions = []

        self.lastMessageTimeThreshold = configDesc.resourceSessionTimeout

    ############################
    def startAccepting(self):
        self.setProtocolFactory(ResourceServerFactory(self))
        GNRServer.startAccepting(self)

    ############################
    def changeResourceDir(self, configDesc):
        if self.dirManager.rootPath == configDesc.rootPath:
            return
        self.dirManager.rootPath = configDesc.rootPath
        self.dirManager.nodeId = configDesc.clientUid
        self.resourceManager.changeResourceDir(self.dirManager.getResourceDir())

    ############################
    def getDistributedResourceRoot(self):
        return self.dirManager.getResourceDir()

    ############################
    def getPeers(self):
        self.client.getResourcePeers()

    ############################
    def addFilesToSend(self, files, taskId, num):
        resFiles = {}
        for file_ in files:
            resFiles[file_] = self.resourceManager.splitFile(file_)
            for res in resFiles[file_]:
                self.addResourceToSend(res, num, taskId)
        return resFiles

    ############################
    def addFilesToGet(self, files, taskId):
        num = 0
        for file_ in files:
            if not self.resourceManager.checkResource(file_):
                num += 1
                self.addResourceToGet(file_, taskId)

        if (num > 0):
            self.waitingTasksToCompute[taskId] = num
        else:
            self.client.taskResourcesCollected(taskId)

    ############################
    def addResourceToSend(self, name, num, taskId = None):
        if taskId not in self.waitingTasks:
            self.waitingTasks[taskId] = 0
        self.resourcesToSend.append([name, taskId, num])
        self.waitingTasks[taskId] += 1

    ############################
    def addResourceToGet(self, name, taskId):
        self.resourcesToGet.append([name, taskId])

    ############################
    def newConnection(self, session):
        session.resourceServer = self
        self.sessions.append(session)

    ############################
    def addResourcePeer(self, clientId, addr, port, keyId, nodeInfo):
        if clientId in self.resourcePeers:
            if self.resourcePeers[clientId]['addr'] == addr and self.resourcePeers[clientId]['port'] == port and self.resourcePeers[clientId]['keyId']:
                return

        self.resourcePeers[clientId] = { 'addr': addr, 'port': port, 'keyId': keyId, 'state': 'free', 'posResource': 0,
                                           'node': nodeInfo}

    ############################
    def setResourcePeers(self, resourcePeers):

        if self.configDesc.clientUid in resourcePeers:
            del resourcePeers[self.configDesc.clientUid]

        for clientId, [addr, port, keyId, nodeInfo] in resourcePeers.iteritems():
            self.addResourcePeer(clientId, addr, port, keyId, nodeInfo)

    ############################
    def syncNetwork(self):
        if len(self.resourcesToGet) + len(self.resourcesToSend) > 0:
            curTime = time.time()
            if curTime - self.lastGetResourcePeersTime > self.getResourcePeersInterval:
                self.client.getResourcePeers()
                self.lastGetResourcePeersTime = time.time()
        self.sendResources()
        self.getResources()
        self.__removeOldSessions()

    ############################
    def getResources(self):
        if len (self.resourcesToGet) == 0:
            return
        resourcePeers = [peer for peer in self.resourcePeers.values() if peer['state'] == 'free']
        random.shuffle(resourcePeers)

        if len (self.resourcePeers) == 0:
            return

        for peer in resourcePeers:
            peer['state'] = 'waiting'
            self.pullResource(self.resourcesToGet[0][0], peer['addr'], peer['port'], peer['keyId'], peer['node'])


    ############################
    def sendResources(self):
        if len(self.resourcesToSend) == 0:
            return

        if self.resSendIt >= len(self.resourcesToSend):
            self.resSendIt = len(self.resourcesToSend) - 1

        resourcePeers = [peer for peer in self.resourcePeers.values() if peer['state'] == 'free']

        for peer in resourcePeers:
            name = self.resourcesToSend[self.resSendIt][0]
            num = self.resourcesToSend[self.resSendIt][2]
            peer['state'] = 'waiting'
            self.pushResource(name , peer['addr'], peer['port'] , peer['keyId'], peer['node'], num)
            self.resSendIt = (self.resSendIt + 1) % len(self.resourcesToSend)

    ############################
    def pullResource(self, resource, addr, port, keyId, nodeInfo):
        # Network.connect(addr, port, ResourceSession, self.__connectionPullResourceEstablished,
        #                 self.__connectionPullResourceFailure, resource, addr, port, keyId)
        hostInfos = nodeInfoToHostInfos(nodeInfo, port)
        addr = self.client.getSuggestedAddr(keyId)
        if addr:
            hostInfos = [HostData(addr, port)] + hostInfos
        self.network.connectToHost(hostInfos, self.__connectionPullResourceEstablished,
                        self.__connectionPullResourceFailure, resource, addr, port, keyId)

    ############################
    def pullAnswer(self, resource, hasResource, session):
        if not hasResource or resource not in [res[0] for res in self.resourcesToGet]:
            self.__freePeer(session.address, session.port)
            session.dropped()
        else:
            if resource not in self.waitingResources:
                self.waitingResources[resource] = []
            for res in self.resourcesToGet:
                if res[0] == resource:
                    self.waitingResources[resource].append(res[1])
            for taskId in self.waitingResources[resource]:
                    self.resourcesToGet.remove([resource, taskId])
            session.fileName = resource
            session.conn.fileMode = True
            session.conn.confirmation = False
            session.sendWantResource(resource)
            if session not in self.sessions:
                self.sessions.append(session)

    ############################
    def pushResource(self, resource, addr, port, keyId, nodeInfo, copies):
        hostInfos = nodeInfoToHostInfos(nodeInfo, port)
        addr = self.client.getSuggestedAddr(keyId)
        if addr:
            hostInfos = [HostData(addr, port)] + hostInfos
        # Network.connect(addr, port, ResourceSession, self.__connectionPushResourceEstablished,
        #                 self.__connectionPushResourceFailure, resource, copies,
        #                 addr, port, keyId)
        self.network.connectToHost(hostInfos, self.__connectionPushResourceEstablished,
                        self.__connectionPushResourceFailure, resource, copies,
                        addr, port, keyId)

    ############################
    def checkResource(self, resource):
        return self.resourceManager.checkResource(resource)

    ############################
    def prepareResource(self, resource):
        return self.resourceManager.getResourcePath(resource)

    ############################
    def resourceDownloaded(self, resource, address, port):
        clientId = self.__freePeer(address, port)
        if not self.resourceManager.checkResource(resource):
            logger.error("Wrong resource downloaded\n")
            if clientId is not None:
                self.client.decreaseTrust(clientId, RankingStats.resource)
            return
        if clientId is not None:
            # Uaktualniamy ranking co 100 zasobow, zeby specjalnie nie zasmiecac sieci
            self.resourcePeers[clientId]['posResource'] += 1
            if (self.resourcePeers[clientId]['posResource'] % 50) == 0:
                self.client.increaseTrust(clientId, RankingStats.resource, 50)
        for taskId in self.waitingResources[resource]:
            self.waitingTasksToCompute[taskId] -= 1
            if self.waitingTasksToCompute[taskId] == 0:
                self.client.taskResourcesCollected(taskId)
                del self.waitingTasksToCompute[taskId]
        del self.waitingResources[resource]

    ############################
    def hasResource(self, resource, addr, port):
        removeRes = False
        for res in self.resourcesToSend:

            if resource == res[0]:
                res[2] -= 1
                if res[2] == 0:
                    removeRes = True
                    taskId = res[1]
                    self.waitingTasks[taskId] -= 1
                    if self.waitingTasks[taskId] == 0:
                        del self.waitingTasks[taskId]
                        if taskId is not None:
                            self.client.taskResourcesSend(taskId)
                    break

        if removeRes:
            self.resourcesToSend.remove([resource, taskId, 0])

        self.__freePeer(addr, port)

    ############################
    def unpackDelta(self, destDir, delta, taskId):
        if not os.path.isdir(destDir):
            os.mkdir(destDir)
        for dirHeader in delta.subDirHeaders:
            self.unpackDelta(os.path.join(destDir, dirHeader.dirName), dirHeader, taskId)

        for filesData in delta.filesData:
            self.resourceManager.connectFile(filesData[2], os.path.join(destDir, filesData[0]))

    ############################
    def removeSession(self, session):
        if session in self.sessions:
            self.__freePeer(session.address, session.port)
            self.sessions.remove(session)

    #############################
    def getKeyId(self):
        return self.keysAuth.getKeyId()

    #############################
    def encrypt(self, message, publicKey):
        if publicKey == 0:
            return message
        return self.keysAuth.encrypt(message, publicKey)

    #############################
    def decrypt(self, message):
        return self.keysAuth.decrypt(message)

    #############################
    def signData(self, data):
        return self.keysAuth.sign(data)

    #############################
    def verifySig(self, sig, data, publicKey):
        return self.keysAuth.verify(sig, data, publicKey)


    ############################
    def changeConfig(self, configDesc):
        self.lastMessageTimeThreshold = configDesc.resourceSessionTimeout

    ############################
    def __freePeer(self, addr, port):
        for clientId, value in self.resourcePeers.iteritems():
            if value['addr'] == addr and value['port'] == port:
                self.resourcePeers[clientId]['state'] = 'free'
                return clientId


    ############################
    def __connectionPushResourceEstablished(self, session, resource, copies, addr, port, keyId):
        session.resourceServer = self
        session.clientKeyId = keyId
        session.sendHello()
        session.sendPushResource(resource, copies)
        self.sessions.append(session)

    ############################
    def __connectionPushResourceFailure(self, resource, copies, addr, port, keyId):
        self.__removeClient(addr, port)
        logger.error("Connection to resource server failed")

    ############################
    def __connectionPullResourceEstablished(self, session, resource, addr, port, keyId):
        session.resourceServer = self
        session.clientKeyId = keyId
        session.sendHello()
        session.sendPullResource(resource)
        self.sessions.append(session)

    ############################
    def __connectionPullResourceFailure(self, resource, addr, port, keyId):
        self.__removeClient(addr, port)
        logger.error("Connection to resource server failed")

    ############################
    def __connectionForResourceEstablished(self, session, resource, addr, port, keyId):
        session.resourceServer = self
        session.clientKeyId = keyId
        session.sendHello()
        session.sendWantResource(resource)
        self.sessions.append(session)

    ############################
    def __connectionForResourceFailure(self, resource, addr, port):
        self.__removeClient(addr, port)
        logger.error("Connection to resource server failed")

    ############################
    def __removeClient(self, addr, port):
        badClient = None
        for clientId, peer in self.resourcePeers.iteritems():
            if peer['addr'] == addr and peer['port'] == port:
                badClient = clientId
                break

        if badClient is not None:
            self.resourcePeers[badClient]

    ############################
    def __removeOldSessions(self):
        curTime = time.time()
        sessionsToRemove = []
        for session in self.sessions:
            if curTime - session.lastMessageTime > self.lastMessageTimeThreshold:
                sessionsToRemove.append(session)
        for session in sessionsToRemove:
            self.removeSession(session)

    ############################
    def _listeningEstablished(self, iListeningPort, *args):
        GNRServer._listeningEstablished(self, iListeningPort, *args)
        self.client.setResourcePort(self.curPort)


##########################################################
from twisted.internet.protocol import Factory

from golem.resource.ResourceSession import ResourceSessionFactory

class ResourceServerFactory(Factory):
    #############################
    def __init__(self, server):
        self.server = server

    #############################
    def buildProtocol(self, addr):
        protocol = NetAndFilesConnState(self.server)
        protocol.setSessionFactory(ResourceSessionFactory())
        return protocol