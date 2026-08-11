package com.swarmradar.app.di

import com.swarmradar.app.data.remote.MqttSwarmClient
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object MqttModule {
    @Provides
    @Singleton
    fun provideMqttSwarmClient(impl: MqttSwarmClient): MqttSwarmClient = impl
}
