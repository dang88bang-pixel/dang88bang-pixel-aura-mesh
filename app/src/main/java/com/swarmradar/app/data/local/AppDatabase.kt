package com.swarmradar.app.data.local

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.TypeConverters
import com.swarmradar.app.data.local.dao.PersonDao
import com.swarmradar.app.data.local.dao.PointCloudDao
import com.swarmradar.app.data.local.dao.SessionDao
import com.swarmradar.app.data.local.dao.TrajectoryDao
import com.swarmradar.app.data.local.entities.Converters
import com.swarmradar.app.data.local.entities.PersonEntity
import com.swarmradar.app.data.local.entities.PointCloudChunkEntity
import com.swarmradar.app.data.local.entities.SessionEntity
import com.swarmradar.app.data.local.entities.TrajectoryPointEntity

@Database(
    entities = [
        SessionEntity::class,
        TrajectoryPointEntity::class,
        PersonEntity::class,
        PointCloudChunkEntity::class
    ],
    version = 1,
    exportSchema = false
)
@TypeConverters(Converters::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun sessionDao(): SessionDao
    abstract fun trajectoryDao(): TrajectoryDao
    abstract fun personDao(): PersonDao
    abstract fun pointCloudDao(): PointCloudDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getInstance(context: Context): AppDatabase =
            INSTANCE ?: synchronized(this) {
                INSTANCE ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "swarm_radar.db"
                )
                    .setJournalMode(RoomDatabase.JournalMode.WRITE_AHEAD_LOGGING) // WAL-Cache
                    .enableWriteAheadLogging()
                    .fallbackToDestructiveMigration()
                    .build()
                    .also { INSTANCE = it }
            }
    }
}
