package com.swarmradar.app.data.remote

import android.content.Context
import com.swarmradar.app.domain.model.EKFState
import com.swarmradar.app.domain.model.Person
import com.swarmradar.app.domain.model.Point3D
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import org.eclipse.paho.android.service.MqttAndroidClient
import org.eclipse.paho.client.mqttv3.IMqttActionListener
import org.eclipse.paho.client.mqttv3.IMqttToken
import org.eclipse.paho.client.mqttv3.MqttCallback
import org.eclipse.paho.client.mqttv3.MqttMessage
import java.util.UUID
import javax.inject.Inject
import javax.inject.Singleton

/**
 * MQTT-Client mit Sparkplug-B-ähnlicher Topic-Struktur für den Schwarm-Datenaustausch.
 * (Hinweis: Payload hier als kompaktes Binärformat; produktiv ggf. echtes Protobuf/Sparkplug B.)
 */
@Singleton
class MqttSwarmClient @Inject constructor(private val context: Context) {

    private val clientId = "swarm_${UUID.randomUUID()}"
    private var mqttClient: MqttAndroidClient? = null

    private val _incoming = MutableSharedFlow<Pair<String, ByteArray>>(extraBufferCapacity = 128)
    val incoming: SharedFlow<Pair<String, ByteArray>> = _incoming.asSharedFlow()

    fun connect(brokerUrl: String) {
        mqttClient = MqttAndroidClient(context, brokerUrl, clientId).also { client ->
            client.setCallback(object : MqttCallback {
                override fun connectionLost(cause: Throwable?) = Unit
                override fun messageArrived(topic: String, message: org.eclipse.paho.client.mqttv3.MqttMessage) {
                    _incoming.tryEmit(topic to message.payload)
                }
                override fun deliveryComplete(token: IMqttToken?) = Unit
            })
            client.connect(null, object : IMqttActionListener {
                override fun onSuccess(asyncActionToken: IMqttToken?) = subscribeToSwarmTopics()
                override fun onFailure(asyncActionToken: IMqttToken?, exception: Throwable?) = Unit
            })
        }
    }

    private fun subscribeToSwarmTopics() {
        mqttClient?.subscribe("spBv1.0/swarm/+/node/data/+", 1)
        mqttClient?.subscribe("spBv1.0/swarm/+/node/cmd/+", 1)
    }

    /** Sendet den lokalen Zustand + Punktwolke (binär kodiert) an den Schwarm. */
    fun publishState(state: EKFState, points: List<Point3D>, persons: List<Person>) {
        val payload = buildPayload(state, points, persons)
        val message = MqttMessage(payload)
        message.qos = 1
        mqttClient?.publish("spBv1.0/swarm/$clientId/node/data", message)
    }

    fun publishCommand(targetClient: String, command: String) {
        val message = MqttMessage(command.toByteArray())
        message.qos = 1
        mqttClient?.publish("spBv1.0/swarm/$targetClient/node/cmd", message)
    }

    private fun buildPayload(
        state: EKFState,
        points: List<Point3D>,
        persons: List<Person>
    ): ByteArray {
        // Kompaktes Format: [x,y,z (3x Float)] + [nPoints (Int)] + [nPoints * (3x Float)] + [nPersons (Int)]
        val out = mutableListOf<Byte>()
        fun putFloat(f: Float) = out.addAll(java.nio.ByteBuffer.allocate(4).putFloat(f).array().toList())
        fun putInt(i: Int) = out.addAll(java.nio.ByteBuffer.allocate(4).putInt(i).array().toList())
        putFloat(state.position.x.toFloat())
        putFloat(state.position.y.toFloat())
        putFloat(state.position.z.toFloat())
        putInt(points.size)
        points.forEach { putFloat(it.x); putFloat(it.y); putFloat(it.z) }
        putInt(persons.size)
        return out.toByteArray()
    }
}
