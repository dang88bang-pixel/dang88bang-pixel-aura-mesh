package com.swarmradar.app.di

import com.swarmradar.app.data.sensor.SensorHardwareManager
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object SensorModule {
    @Provides
    @Singleton
    fun provideSensorHardwareManager(impl: SensorHardwareManager): SensorHardwareManager = impl
}
