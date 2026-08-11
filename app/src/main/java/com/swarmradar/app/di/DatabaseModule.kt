package com.swarmradar.app.di

import android.content.Context
import com.swarmradar.app.data.local.AppDatabase
import com.swarmradar.app.data.local.dao.PersonDao
import com.swarmradar.app.data.local.dao.PointCloudDao
import com.swarmradar.app.data.local.dao.SessionDao
import com.swarmradar.app.data.local.dao.TrajectoryDao
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object DatabaseModule {

    @Provides
    @Singleton
    fun provideAppDatabase(@ApplicationContext context: Context): AppDatabase =
        AppDatabase.getInstance(context)

    @Provides
    @Singleton
    fun provideSessionDao(db: AppDatabase): SessionDao = db.sessionDao()

    @Provides
    @Singleton
    fun provideTrajectoryDao(db: AppDatabase): TrajectoryDao = db.trajectoryDao()

    @Provides
    @Singleton
    fun providePersonDao(db: AppDatabase): PersonDao = db.personDao()

    @Provides
    @Singleton
    fun providePointCloudDao(db: AppDatabase): PointCloudDao = db.pointCloudDao()
}
