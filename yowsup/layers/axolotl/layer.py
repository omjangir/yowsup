from yowsup.layers import YowLayer, YowLayerEvent
from protocolentities import SetKeysIqProtocolEntity
from axolotl.util.keyhelper import KeyHelper
from store.sqlite.liteaxolotlstore import LiteAxolotlStore
from axolotl.sessionbuilder import SessionBuilder
from yowsup.layers.protocol_messages.protocolentities.message import MessageProtocolEntity
from yowsup.layers.network.layer import YowNetworkLayer
from yowsup.layers.auth.layer_authentication import YowAuthenticationProtocolLayer
from axolotl.ecc.curve import Curve
from yowsup.common.tools import StorageTools
from yowsup.common.constants import YowConstants
from axolotl.protocol.prekeywhispermessage import PreKeyWhisperMessage
from axolotl.protocol.whispermessage import WhisperMessage
from .protocolentities import EncryptedMessageProtocolEntity
from axolotl.sessioncipher import SessionCipher
from yowsup.structs import ProtocolTreeNode
from .protocolentities import GetKeysIqProtocolEntity, ResultGetKeysIqProtocolEntity
from yowsup.env import CURRENT_ENV
import binascii

import logging
logger = logging.getLogger(__name__)


class YowAxolotlLayer(YowLayer):
    EVENT_PREKEYS_SET = "org.openwhatsapp.yowsup.events.axololt.setkeys"
    _STATE_INIT = 0
    _STATE_GENKEYS = 1
    _STATE_HASKEYS = 2
    _DB = "axolotl.db"

    def __init__(self):
        super(YowAxolotlLayer, self).__init__()
        self.store = None
        self.state = self.__class__._STATE_INIT

        self.pendingKeys = None
        self.sessionCiphers = {}
        self.pendingMessages = {}
        self.pendingGetKeysIqs = {}
        self.skipEncJids = []

    def __str__(self):
        return "Axolotl Layer"

    def send(self, node):
        if node.tag == "message" and node["type"] == "text" and node["to"] not in self.skipEncJids:
            self.handlePlaintextNode(node)
            return
        self.toLower(node)

    def receive(self, protocolTreeNode):
        """
        :type protocolTreeNode: ProtocolTreeNode
        """
        if protocolTreeNode.tag == "iq":
            if self.pendingKeys and self.pendingKeys["iq"] == protocolTreeNode["id"]:
                logger.info("Persisting keys, this will take a minute")
                self.store.storeLocalData(self.pendingKeys["registrationId"], self.pendingKeys["identityKeyPair"])
                logger.debug("Stored RegistrationId, and IdentityKeyPair")
                self.store.storeSignedPreKey(self.pendingKeys["signedPreKey"].getId(), self.pendingKeys["signedPreKey"])
                logger.debug("Stored Signed PreKey")
                total = len(self.pendingKeys["preKeys"])
                curr = 0
                prevPercentage = 0
                for preKey in self.pendingKeys["preKeys"]:
                    self.store.storePreKey(preKey.getId(), preKey)
                    curr += 1
                    currPercentage = (curr * 100) / total
                    if currPercentage == prevPercentage:
                        continue
                    prevPercentage = currPercentage
                    logger.debug("%s" % currPercentage + "%")

                self.state = self.__class__._STATE_GENKEYS
                self.broadcastEvent(YowLayerEvent(YowNetworkLayer.EVENT_STATE_DISCONNECT))

                return
            elif protocolTreeNode["id"] in self.pendingGetKeysIqs:
                recipient_id = self.pendingGetKeysIqs[protocolTreeNode["id"]]
                jid = recipient_id + "@s.whatsapp.net"
                del self.pendingGetKeysIqs[protocolTreeNode["id"]]
                entity = ResultGetKeysIqProtocolEntity.fromProtocolTreeNode(protocolTreeNode)
                preKeyBundle = entity.getPreKeyBundleFor(jid)
                if preKeyBundle:
                    sessionBuilder = SessionBuilder(self.store, self.store, self.store,
                                                   self.store, recipient_id, 1)
                    sessionBuilder.processPreKeyBundle(preKeyBundle)

                    self.processPendingMessages(jid)
                else:
                    self.skipEncJids.append(jid)
                    self.processPendingMessages(jid)


                #registrationId =
                #preKeyBundle = PreKeyBundle()


                return
        elif protocolTreeNode.tag == "message":
            encNode = protocolTreeNode.getChild("enc")
            if encNode:
                self.handleEncMessage(protocolTreeNode)
                return
        self.toUpper(protocolTreeNode)

    def processPendingMessages(self, jid):
        if jid in self.pendingMessages:
            for messageNode in self.pendingMessages[jid]:
                if jid in self.skipEncJids:
                    self.toLower(messageNode)
                else:
                    self.handlePlaintextNode(messageNode)

            del self.pendingMessages[jid]

    def onEvent(self, yowLayerEvent):
        if yowLayerEvent.getName() == self.__class__.EVENT_PREKEYS_SET:
            self.sendKeys()
        elif yowLayerEvent.getName() == YowNetworkLayer.EVENT_STATE_CONNECT:
            self.initStore()
            if self.isInitState():
                self.setProp(YowAuthenticationProtocolLayer.PROP_PASSIVE, True)
        elif yowLayerEvent.getName() == YowAuthenticationProtocolLayer.EVENT_AUTHED:
            if yowLayerEvent.getArg("passive") and self.isInitState():
                logger.info("Textsecure layer is generating keys, this might take a minute")
                self._sendKeys()
        elif yowLayerEvent.getName() == YowNetworkLayer.EVENT_STATE_DISCONNECTED:
            if self.isGenKeysState():
                #we requested this disconnect in this layer to switch off passive
                #no need to traverse it to upper layers?
                self.setProp(YowAuthenticationProtocolLayer.PROP_PASSIVE, False)
                self.state = self.__class__._STATE_HASKEYS
                self.broadcastEvent(YowLayerEvent(YowNetworkLayer.EVENT_STATE_CONNECT))


    def initStore(self):
        self.store = LiteAxolotlStore(
            StorageTools.constructPath(
                self.getProp(
                    YowAuthenticationProtocolLayer.PROP_CREDENTIALS)[0],
                self.__class__._DB
            )
        )
        self.state = self.__class__._STATE_HASKEYS if  self.store.getLocalRegistrationId() is not None \
            else self.__class__._STATE_INIT
        self.pendingKeys = None



    def isInitState(self):
        return self.state == self.__class__._STATE_INIT

    def isGenKeysState(self):
        return self.state == self.__class__._STATE_GENKEYS

    def adjustArray(self, arr):
        return binascii.hexlify(arr).decode('hex')


    def handlePlaintextNode(self, node):
        plaintext = node.getChild("body").getData()
        entity = MessageProtocolEntity.fromProtocolTreeNode(node)
        recipient_id = entity.getTo(False)

        if not self.store.containsSession(recipient_id, 1):
            entity = GetKeysIqProtocolEntity(node["to"])
            if node["to"] not in self.pendingMessages:
                self.pendingMessages[node["to"]] = []
            self.pendingMessages[node["to"]].append(node)
            self.pendingGetKeysIqs[entity.getId()] = recipient_id
            self.toLower(entity.toProtocolTreeNode())
        else:

            sessionCipher = self.getSessionCipher(recipient_id)

            ciphertext = sessionCipher.encrypt(bytearray(plaintext))
            encEntity = EncryptedMessageProtocolEntity(
                EncryptedMessageProtocolEntity.TYPE_MSG if ciphertext.__class__ == WhisperMessage else EncryptedMessageProtocolEntity.TYPE_PKMSG ,
                                                   "%s/%s" % (CURRENT_ENV.getOSName(), CURRENT_ENV.getVersion()),
                                                   1,
                                                   ciphertext.serialize(),
                                                   MessageProtocolEntity.MESSAGE_TYPE_TEXT,
                                                   _id= node["id"],
                                                   to = node["to"],
                                                   notify = node["notify"],
                                                   timestamp= node["timestamp"],
                                                   participant=node["participant"],
                                                   offline=node["offline"],
                                                   retry=node["retry"]
                                                   )
            self.toLower(encEntity.toProtocolTreeNode())



    def handleEncMessage(self, node):
        if node.getChild("enc")["type"] == "pkmsg":
            self.handlePreKeyWhisperMessage(node)
        else:
            self.handleWhisperMessage(node)

    def getSessionCipher(self, recipientId):
        if recipientId in self.sessionCiphers:
            return self.sessionCiphers[recipientId]
        else:
            self.sessionCiphers[recipientId] = SessionCipher(self.store, self.store, self.store, self.store, recipientId, 1)
            return self.sessionCiphers[recipientId]

    def handlePreKeyWhisperMessage(self, node):
        pkMessageProtocolEntity = EncryptedMessageProtocolEntity.fromProtocolTreeNode(node)

        preKeyWhisperMessage = PreKeyWhisperMessage(serialized=pkMessageProtocolEntity.getEncData())
        sessionCipher = self.getSessionCipher(pkMessageProtocolEntity.getFrom(False))
        plaintext = sessionCipher.decryptPkmsg(preKeyWhisperMessage)

        bodyNode = ProtocolTreeNode("body", data = plaintext)
        node.addChild(bodyNode)
        self.toUpper(node)

    def handleWhisperMessage(self, node):
        encMessageProtocolEntity = EncryptedMessageProtocolEntity.fromProtocolTreeNode(node)

        whisperMessage = WhisperMessage(serialized=encMessageProtocolEntity.getEncData())
        sessionCipher = self.getSessionCipher(encMessageProtocolEntity.getFrom(False))
        plaintext = sessionCipher.decryptMsg(whisperMessage)

        bodyNode = ProtocolTreeNode("body", data = plaintext)
        node.addChild(bodyNode)
        self.toUpper(node)

    def adjustId(self, _id):
        _id = format(_id, 'x').zfill(6)
        # if len(_id) % 2:
        #     _id = "0" + _id
        return binascii.unhexlify(_id)


    def xxx_sendKeys(self):
        self.genKeys()
        return
        identityKeyPair = self.store.getIdentityKeyPair()
        registrationId = self.store.getLocalRegistrationId()
        preKeys = self.store.loadPreKeys()[:2]
        signedPreKey = self.store.loadSignedPreKeys()[0]

        preKeysDict = {}
        for preKey in preKeys:
            keyPair = preKey.getKeyPair()
            preKeysDict[self.adjustId(preKey.getId())] = self.adjustArray(keyPair.getPublicKey().serialize()[1:])

        signedKeyTuple = (self.adjustId(signedPreKey.getId()),
                          self.adjustArray(signedPreKey.getKeyPair().getPublicKey().serialize()[1:]),
                          self.adjustArray(signedPreKey.getSignature()))


        setKeysIq = SetKeysIqProtocolEntity(self.adjustArray(identityKeyPair.getPublicKey().serialize()[1:]), signedKeyTuple, preKeysDict, Curve.DJB_TYPE, self.adjustId(registrationId))


        print(setKeysIq.toProtocolTreeNode())

        # self.toLower(setKeysIq.toProtocolTreeNode())


    def _sendKeys(self):
        logger.debug("Generating Identity...")
        identityKeyPair     = KeyHelper.generateIdentityKeyPair()
        logger.debug("Generating Registration Id...")
        registrationId      = KeyHelper.generateRegistrationId()
        logger.debug("Generating 200 PreKeys...")
        preKeys             = KeyHelper.generatePreKeys(7493876, 50)
        logger.debug("Generating Signed PreKey")
        signedPreKey        = KeyHelper.generateSignedPreKey(identityKeyPair, 0)
        logger.debug("Preparing payload")
        preKeysDict = {}
        for preKey in preKeys:
            keyPair = preKey.getKeyPair()
            preKeysDict[self.adjustId(preKey.getId())] = self.adjustArray(keyPair.getPublicKey().serialize()[1:])

        signedKeyTuple = (self.adjustId(signedPreKey.getId()),
                          self.adjustArray(signedPreKey.getKeyPair().getPublicKey().serialize()[1:]),
                          self.adjustArray(signedPreKey.getSignature()))

        print registrationId

        setKeysIq = SetKeysIqProtocolEntity(self.adjustArray(identityKeyPair.getPublicKey().serialize()[1:]), signedKeyTuple, preKeysDict, Curve.DJB_TYPE, self.adjustId(registrationId))

        self.pendingKeys = {
            "iq": setKeysIq.getId(),
            "identityKeyPair": identityKeyPair,
            "registrationId": registrationId,
            "preKeys": preKeys,
            "signedPreKey": signedPreKey
        }

        logger.debug("Dropping payload")
        self.toLower(setKeysIq.toProtocolTreeNode())


    def genKeys(self):
        identityKeyPair     = KeyHelper.generateIdentityKeyPair()
        registrationId      = KeyHelper.generateRegistrationId()
        preKeys             = KeyHelper.generatePreKeys(7493876, 200)
        signedPreKey        = KeyHelper.generateSignedPreKey(identityKeyPair, 0)


        self.store.storeLocalData(registrationId, identityKeyPair)
        self.store.storeSignedPreKey(signedPreKey.getId(), signedPreKey)

        preKeysDict = {}
        for preKey in preKeys:
            self.store.storePreKey(preKey.getId(), preKey)
            keyPair = preKey.getKeyPair()
            preKeysDict[self.adjustId(preKey.getId())] = self.adjustArray(keyPair.getPublicKey().serialize()[1:])

        signedKeyTuple = (self.adjustId(signedPreKey.getId()),
                          self.adjustArray(signedPreKey.getKeyPair().getPublicKey().serialize()[1:]),
                          self.adjustArray(signedPreKey.getSignature()))


        print registrationId
        setKeysIq = SetKeysIqProtocolEntity(self.adjustArray(identityKeyPair.getPublicKey().serialize()[1:]), signedKeyTuple, preKeysDict, self.__class__.TYPE_DJB, self.adjustId(registrationId))
        self.toLower(setKeysIq.toProtocolTreeNode())

