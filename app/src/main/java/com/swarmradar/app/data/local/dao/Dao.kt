package com.swarmradar.app.data.local.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query
import com.swarmradar.app.data.local.entities.PersonEntity
import com.swarmradar.app.data.local.entities.PointCloudChunkEntity
import com.swarmradar.app.data.local.entities.SessionEntity
import com.swarmradar.app.data.local.entities.TrajectoryPointEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface SessionDao {
    @Insert
    suspend fun insertSession(s: SessionEntity): Long

    @Query("SELECT * FROM sessions WHERE id = :id")
    suspend fun getSession(id: Long): SessionEntity?

    @Query("SELECT * FROM sessions ORDER BY startTime DESC")
    fun observeSessions(): Flow<List<SessionEntity>>
}

@Dao
interface TrajectoryDao {
    @Insert
    suspend fun insertPoint(p: TrajectoryPointEntity)

    @Query("SELECT * FROM trajectory WHERE sessionId = :sessionId ORDER BY timestamp")
    fun getPointsForSession(sessionId: Long): Flow<List<TrajectoryPointEntity>>
}

@Dao
interface PersonDao {
    @Insert
    suspend fun insertPerson(p: PersonEntity)

    @Query("SELECT * FROM persons WHERE sessionId = :sessionId")
    fun getPersons(sessionId: Long): Flow<List<PersonEntity>>
}

@Dao
interface PointCloudDao {
    @Insert
    suspend fun insertChunk(c: PointCloudChunkEntity)

    @Query("SELECT * FROM pointcloud_chunks WHERE sessionId = :sessionId")
    suspend fun getChunks(sessionId: Long): List<PointCloudChunkEntity>
}
