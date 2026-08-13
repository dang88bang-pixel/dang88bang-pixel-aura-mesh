package com.aura.agent.storage

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import kotlin.math.floor

/**
 * On-device context store: SQLite in WAL mode.
 *
 * Schema-compatible with `edge-agent/aura/storage.py`, so a CT45P can sync a
 * recording up to the agent (and pull one back) without any translation. WAL
 * matters here: the fusion thread writes at 4-20 Hz while the UI reads for the
 * map, and the default rollback journal would serialise them into visible
 * frame drops.
 */
class LocalVectorStore(
    context: Context,
    private val projectName: String = "default",
) : SQLiteOpenHelper(context, DATABASE_NAME, null, DATABASE_VERSION) {

    var projectId: String = ""
        private set

    init {
        writableDatabase.apply {
            enableWriteAheadLogging()
            execSQL("PRAGMA synchronous=NORMAL")
            execSQL("PRAGMA foreign_keys=ON")
        }
        projectId = ensureProject(projectName)
    }

    override fun onCreate(db: SQLiteDatabase) {
        SCHEMA.split(";").filter { it.isNotBlank() }.forEach { db.execSQL(it) }
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // Recordings are reproducible field data, not user documents: on a
        // schema break we drop and rebuild rather than risk a partial migration
        // on a device that may be mid-deployment.
        db.execSQL("DROP TABLE IF EXISTS transforms")
        db.execSQL("DROP TABLE IF EXISTS events")
        db.execSQL("DROP TABLE IF EXISTS spatial_chunks")
        db.execSQL("DROP TABLE IF EXISTS maps")
        db.execSQL("DROP TABLE IF EXISTS projects")
        db.execSQL("DROP TABLE IF EXISTS audit_log")
        onCreate(db)
    }

    override fun onConfigure(db: SQLiteDatabase) {
        super.onConfigure(db)
        db.setForeignKeyConstraintsEnabled(true)
    }

    // ------------------------------------------------------------------
    fun ensureProject(name: String, metadata: JSONObject = JSONObject()): String {
        val db = writableDatabase
        db.rawQuery("SELECT project_id FROM projects WHERE name = ?", arrayOf(name)).use { cursor ->
            if (cursor.moveToFirst()) {
                val id = cursor.getString(0)
                db.execSQL(
                    "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                    arrayOf(now(), id),
                )
                return id
            }
        }
        val id = "prj-" + UUID.randomUUID().toString().replace("-", "").take(12)
        db.insert("projects", null, ContentValues().apply {
            put("project_id", id)
            put("name", name)
            put("created_at", now())
            put("updated_at", now())
            put("version", 1)
            put("metadata", metadata.toString())
        })
        return id
    }

    // ------------------------------------------------------------------
    fun saveTransform(
        transform: Transform3D,
        covariance: FloatArray = FloatArray(0),
        velocity: FloatArray = FloatArray(0),
        metadata: JSONObject = JSONObject(),
        timestamp: Long = System.currentTimeMillis(),
    ): String {
        val recordId = "trf-" + UUID.randomUUID().toString().replace("-", "").take(14)
        writableDatabase.insert("transforms", null, ContentValues().apply {
            put("record_id", recordId)
            put("project_id", projectId)
            put("timestamp", timestamp / 1000.0)
            put("offset_x", transform.x)
            put("offset_y", transform.y)
            put("offset_z", transform.z)
            put("roll", transform.roll)
            put("pitch", transform.pitch)
            put("yaw", transform.yaw)
            put("velocity", velocity.toJsonArray())
            put("covariance", covariance.toJsonArray())
            put("metadata", metadata.toString())
        })
        return recordId
    }

    fun history(limit: Int = 500, since: Long? = null): List<TransformRecord> {
        val out = mutableListOf<TransformRecord>()
        val args = mutableListOf<String>(projectId)
        var sql = "SELECT * FROM transforms WHERE project_id = ?"
        if (since != null) {
            sql += " AND timestamp >= ?"
            args += (since / 1000.0).toString()
        }
        sql += " ORDER BY timestamp DESC LIMIT ?"
        args += limit.toString()

        readableDatabase.rawQuery(sql, args.toTypedArray()).use { cursor ->
            while (cursor.moveToNext()) {
                out += TransformRecord(
                    recordId = cursor.getString(cursor.getColumnIndexOrThrow("record_id")),
                    timestamp = (cursor.getDouble(cursor.getColumnIndexOrThrow("timestamp")) * 1000).toLong(),
                    transform = Transform3D(
                        x = cursor.getFloat(cursor.getColumnIndexOrThrow("offset_x")),
                        y = cursor.getFloat(cursor.getColumnIndexOrThrow("offset_y")),
                        z = cursor.getFloat(cursor.getColumnIndexOrThrow("offset_z")),
                        roll = cursor.getFloat(cursor.getColumnIndexOrThrow("roll")),
                        pitch = cursor.getFloat(cursor.getColumnIndexOrThrow("pitch")),
                        yaw = cursor.getFloat(cursor.getColumnIndexOrThrow("yaw")),
                    ),
                    metadata = runCatching {
                        JSONObject(cursor.getString(cursor.getColumnIndexOrThrow("metadata")))
                    }.getOrDefault(JSONObject()),
                )
            }
        }
        return out.reversed()
    }

    fun saveEvent(source: String, payload: JSONObject, timestamp: Long = System.currentTimeMillis()): String {
        val eventId = "evt-" + UUID.randomUUID().toString().replace("-", "").take(14)
        writableDatabase.insert("events", null, ContentValues().apply {
            put("event_id", eventId)
            put("project_id", projectId)
            put("timestamp", timestamp / 1000.0)
            put("source", source)
            put("payload", payload.toString())
        })
        return eventId
    }

    // ------------------------------------------------------------------
    // voxel chunks
    // ------------------------------------------------------------------
    fun saveChunk(cx: Int, cy: Int, cz: Int, blob: ByteArray, timestamp: Long = System.currentTimeMillis()) {
        writableDatabase.insertWithOnConflict(
            "spatial_chunks", null,
            ContentValues().apply {
                put("chunk_x", cx)
                put("chunk_y", cy)
                put("chunk_z", cz)
                put("project_id", projectId)
                put("timestamp", timestamp / 1000.0)
                put("voxel_data", blob)
            },
            SQLiteDatabase.CONFLICT_REPLACE,
        )
    }

    fun loadChunk(cx: Int, cy: Int, cz: Int): ByteArray? {
        readableDatabase.rawQuery(
            "SELECT voxel_data FROM spatial_chunks WHERE project_id = ? AND chunk_x = ? AND chunk_y = ? AND chunk_z = ?",
            arrayOf(projectId, cx.toString(), cy.toString(), cz.toString()),
        ).use { cursor ->
            return if (cursor.moveToFirst()) cursor.getBlob(0) else null
        }
    }

    /** Load every chunk whose centre lies within [radius] metres of the pose. */
    fun chunksInRadius(x: Float, y: Float, z: Float, radius: Float, chunkExtent: Float): List<ChunkRecord> {
        val min = { v: Float -> floor((v - radius) / chunkExtent).toInt() }
        val max = { v: Float -> floor((v + radius) / chunkExtent).toInt() }
        val out = mutableListOf<ChunkRecord>()
        readableDatabase.rawQuery(
            "SELECT chunk_x, chunk_y, chunk_z, voxel_data, timestamp FROM spatial_chunks" +
                " WHERE project_id = ? AND chunk_x BETWEEN ? AND ? AND chunk_y BETWEEN ? AND ?" +
                " AND chunk_z BETWEEN ? AND ?",
            arrayOf(
                projectId,
                min(x).toString(), max(x).toString(),
                min(y).toString(), max(y).toString(),
                min(z).toString(), max(z).toString(),
            ),
        ).use { cursor ->
            while (cursor.moveToNext()) {
                out += ChunkRecord(
                    cursor.getInt(0), cursor.getInt(1), cursor.getInt(2),
                    cursor.getBlob(3),
                    (cursor.getDouble(4) * 1000).toLong(),
                )
            }
        }
        return out
    }

    // ------------------------------------------------------------------
    /**
     * Delete rows that are too old or beyond the record cap.
     *
     * Called periodically by the fusion service: a full-day survey at 10 Hz
     * would otherwise fill the device's storage.
     */
    fun enforceRetention(maxAgeSeconds: Long, maxRecords: Int): RetentionResult {
        val db = writableDatabase
        val cutoff = (System.currentTimeMillis() / 1000.0) - maxAgeSeconds
        var transforms = db.delete("transforms", "timestamp < ?", arrayOf(cutoff.toString()))
        var events = db.delete("events", "timestamp < ?", arrayOf(cutoff.toString()))

        transforms += db.delete(
            "transforms",
            "record_id IN (SELECT record_id FROM transforms ORDER BY timestamp DESC LIMIT -1 OFFSET ?)",
            arrayOf(maxRecords.toString()),
        )
        events += db.delete(
            "events",
            "event_id IN (SELECT event_id FROM events ORDER BY timestamp DESC LIMIT -1 OFFSET ?)",
            arrayOf(maxRecords.toString()),
        )
        return RetentionResult(transforms, events)
    }

    fun stats(): JSONObject {
        val db = readableDatabase
        fun count(table: String): Int =
            db.rawQuery("SELECT COUNT(*) FROM $table", null).use { it.moveToFirst(); it.getInt(0) }

        val pageSize = db.rawQuery("PRAGMA page_size", null).use { it.moveToFirst(); it.getLong(0) }
        val pageCount = db.rawQuery("PRAGMA page_count", null).use { it.moveToFirst(); it.getLong(0) }
        val journal = db.rawQuery("PRAGMA journal_mode", null).use { it.moveToFirst(); it.getString(0) }

        return JSONObject().apply {
            put("projects", count("projects"))
            put("transforms", count("transforms"))
            put("events", count("events"))
            put("chunks", count("spatial_chunks"))
            put("size_bytes", pageSize * pageCount)
            put("journal_mode", journal)
        }
    }

    private fun now(): Double = System.currentTimeMillis() / 1000.0

    private fun FloatArray.toJsonArray(): String =
        JSONArray().also { array -> forEach { array.put(it.toDouble()) } }.toString()

    companion object {
        private const val DATABASE_NAME = "aura.db"
        private const val DATABASE_VERSION = 1

        private val SCHEMA = """
            CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS transforms (
                record_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                offset_x REAL NOT NULL, offset_y REAL NOT NULL, offset_z REAL NOT NULL,
                roll REAL NOT NULL, pitch REAL NOT NULL, yaw REAL NOT NULL,
                velocity TEXT NOT NULL DEFAULT '[]',
                covariance TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_transforms_ts ON transforms(project_id, timestamp DESC);
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                source TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(project_id, timestamp DESC);
            CREATE TABLE IF NOT EXISTS spatial_chunks (
                chunk_x INTEGER NOT NULL,
                chunk_y INTEGER NOT NULL,
                chunk_z INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                voxel_data BLOB NOT NULL,
                PRIMARY KEY (project_id, chunk_x, chunk_y, chunk_z)
            );
            CREATE TABLE IF NOT EXISTS maps (
                map_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                version INTEGER NOT NULL,
                resolution REAL NOT NULL,
                origin_x REAL NOT NULL, origin_y REAL NOT NULL,
                cells INTEGER NOT NULL,
                grid BLOB NOT NULL,
                stats TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                idx INTEGER PRIMARY KEY,
                entry_id TEXT NOT NULL UNIQUE,
                timestamp REAL NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                chain_hash TEXT NOT NULL
            );
        """.trimIndent()
    }
}

data class Transform3D(
    val x: Float, val y: Float, val z: Float,
    val roll: Float, val pitch: Float, val yaw: Float,
)

data class TransformRecord(
    val recordId: String,
    val timestamp: Long,
    val transform: Transform3D,
    val metadata: JSONObject,
)

data class ChunkRecord(
    val cx: Int, val cy: Int, val cz: Int,
    val data: ByteArray,
    val timestamp: Long,
) {
    override fun equals(other: Any?): Boolean =
        other is ChunkRecord && cx == other.cx && cy == other.cy && cz == other.cz

    override fun hashCode(): Int = (cx * 31 + cy) * 31 + cz
}

data class RetentionResult(val transforms: Int, val events: Int)
