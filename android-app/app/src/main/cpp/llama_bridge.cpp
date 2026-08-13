// llama_bridge.cpp - JNI bridge for the optional offline assistant.
//
// This is a SEPARATE shared library (libllama_bridge.so) from libaura_core.so,
// on purpose:
//
//   * llama.cpp is a large third-party dependency that must be vendored
//     (git submodule or FetchContent). The sensor pipeline must not depend on
//     it — a survey device with no model installed has to boot and map
//     normally, and it does: LLMService wraps System.loadLibrary in
//     runCatching, so an absent library degrades to "assistant unavailable".
//   * It lets CI build and test the core without a 200 MB dependency.
//
// Two build modes:
//
//   -DAURA_WITH_LLAMA=ON   real llama.cpp; requires the vendored sources
//   (default)              stub that returns honest "not compiled in" errors
//
// The stub exists so the JNI symbols resolve. Without it, every LLMService
// call throws UnsatisfiedLinkError, which is indistinguishable at the call
// site from a genuine crash. tools/check-jni-symbols.py enforces that every
// `external fun` has a symbol, and this file is how the LLM ones get theirs.

#include <jni.h>

#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#ifdef AURA_WITH_LLAMA
#include "llama.h"
#endif

namespace {

std::mutex g_mutex;
jlong g_next_handle = 1;

struct Session {
    std::string model_path;
    int threads = 4;
    int context_size = 2048;
#ifdef AURA_WITH_LLAMA
    llama_model* model = nullptr;
    llama_context* ctx = nullptr;
#endif
};

std::unordered_map<jlong, Session*> g_sessions;

Session* session_for(jlong handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_sessions.find(handle);
    return it == g_sessions.end() ? nullptr : it->second;
}

jstring to_jstring(JNIEnv* env, const std::string& text) {
    return env->NewStringUTF(text.c_str());
}

std::string from_jstring(JNIEnv* env, jstring value) {
    if (value == nullptr) return {};
    const char* raw = env->GetStringUTFChars(value, nullptr);
    std::string out = raw ? raw : "";
    env->ReleaseStringUTFChars(value, raw);
    return out;
}

}  // namespace

extern "C" {

JNIEXPORT jlong JNICALL
Java_com_aura_agent_llm_LLMService_nativeInit(JNIEnv* env, jobject, jstring modelPath,
                                              jint threads, jint contextSize) {
    const std::string path = from_jstring(env, modelPath);
    if (path.empty()) return 0;

    auto* session = new Session();
    session->model_path = path;
    session->threads = threads > 0 ? threads : 4;
    session->context_size = contextSize > 0 ? contextSize : 2048;

#ifdef AURA_WITH_LLAMA
    llama_backend_init();

    llama_model_params model_params = llama_model_default_params();
    // CPU only: the QCS4290 has no usable GPU offload path for llama.cpp, and
    // attempting it costs more in memory pressure than it saves in latency.
    model_params.n_gpu_layers = 0;
    model_params.use_mmap = true;   // keeps a 1-2 GB model out of the heap

    session->model = llama_model_load_from_file(path.c_str(), model_params);
    if (session->model == nullptr) {
        delete session;
        return 0;
    }

    llama_context_params ctx_params = llama_context_default_params();
    ctx_params.n_ctx = static_cast<uint32_t>(session->context_size);
    ctx_params.n_threads = session->threads;
    ctx_params.n_threads_batch = session->threads;

    session->ctx = llama_init_from_model(session->model, ctx_params);
    if (session->ctx == nullptr) {
        llama_model_free(session->model);
        delete session;
        return 0;
    }
#else
    // Stub mode: no model is loaded. Return 0 so LLMService.isLoaded stays
    // false and the UI reports "assistant unavailable" rather than pretending.
    delete session;
    return 0;
#endif

    std::lock_guard<std::mutex> lock(g_mutex);
    const jlong handle = g_next_handle++;
    g_sessions[handle] = session;
    return handle;
}

JNIEXPORT jstring JNICALL
Java_com_aura_agent_llm_LLMService_nativeGenerate(JNIEnv* env, jobject, jlong handle,
                                                  jstring prompt, jint maxTokens) {
    Session* session = session_for(handle);
    if (session == nullptr) {
        return to_jstring(env, "Modell nicht geladen (llama_bridge ohne AURA_WITH_LLAMA gebaut).");
    }

#ifdef AURA_WITH_LLAMA
    const std::string text = from_jstring(env, prompt);
    const llama_vocab* vocab = llama_model_get_vocab(session->model);

    const int n_prompt = -llama_tokenize(vocab, text.c_str(), static_cast<int32_t>(text.size()),
                                         nullptr, 0, true, true);
    if (n_prompt <= 0) return to_jstring(env, "");

    std::vector<llama_token> tokens(static_cast<size_t>(n_prompt));
    if (llama_tokenize(vocab, text.c_str(), static_cast<int32_t>(text.size()),
                       tokens.data(), n_prompt, true, true) < 0) {
        return to_jstring(env, "Tokenisierung fehlgeschlagen.");
    }

    auto sampler_params = llama_sampler_chain_default_params();
    llama_sampler* sampler = llama_sampler_chain_init(sampler_params);
    // Low temperature: this assistant answers questions about measurements.
    // Creative sampling here means confabulated numbers, which is the one
    // failure mode that would make the feature actively dangerous.
    llama_sampler_chain_add(sampler, llama_sampler_init_top_k(40));
    llama_sampler_chain_add(sampler, llama_sampler_init_temp(0.3f));
    llama_sampler_chain_add(sampler, llama_sampler_init_dist(LLAMA_DEFAULT_SEED));

    std::string result;
    llama_batch batch = llama_batch_get_one(tokens.data(), static_cast<int32_t>(tokens.size()));

    const int limit = maxTokens > 0 ? maxTokens : 256;
    for (int generated = 0; generated < limit; ++generated) {
        if (llama_decode(session->ctx, batch) != 0) break;

        const llama_token next = llama_sampler_sample(sampler, session->ctx, -1);
        if (llama_vocab_is_eog(vocab, next)) break;

        char piece[256];
        const int written = llama_token_to_piece(vocab, next, piece, sizeof(piece), 0, true);
        if (written > 0) result.append(piece, static_cast<size_t>(written));

        batch = llama_batch_get_one(const_cast<llama_token*>(&next), 1);
    }

    llama_sampler_free(sampler);
    return to_jstring(env, result);
#else
    (void) prompt;
    (void) maxTokens;
    return to_jstring(env, "Assistent nicht verfuegbar: mit -DAURA_WITH_LLAMA=ON neu bauen.");
#endif
}

JNIEXPORT jfloatArray JNICALL
Java_com_aura_agent_llm_LLMService_nativeEmbed(JNIEnv* env, jobject, jlong handle, jstring text) {
    Session* session = session_for(handle);
    if (session == nullptr) return env->NewFloatArray(0);

#ifdef AURA_WITH_LLAMA
    const std::string input = from_jstring(env, text);
    const llama_vocab* vocab = llama_model_get_vocab(session->model);

    const int n_tokens = -llama_tokenize(vocab, input.c_str(), static_cast<int32_t>(input.size()),
                                         nullptr, 0, true, true);
    if (n_tokens <= 0) return env->NewFloatArray(0);

    std::vector<llama_token> tokens(static_cast<size_t>(n_tokens));
    llama_tokenize(vocab, input.c_str(), static_cast<int32_t>(input.size()),
                   tokens.data(), n_tokens, true, true);

    llama_batch batch = llama_batch_get_one(tokens.data(), static_cast<int32_t>(tokens.size()));
    if (llama_decode(session->ctx, batch) != 0) return env->NewFloatArray(0);

    const int n_embd = llama_model_n_embd(session->model);
    const float* embeddings = llama_get_embeddings_seq(session->ctx, 0);
    if (embeddings == nullptr) embeddings = llama_get_embeddings(session->ctx);
    if (embeddings == nullptr) return env->NewFloatArray(0);

    jfloatArray out = env->NewFloatArray(n_embd);
    if (out == nullptr) return nullptr;
    env->SetFloatArrayRegion(out, 0, n_embd, embeddings);
    return out;
#else
    (void) text;
    return env->NewFloatArray(0);
#endif
}

JNIEXPORT void JNICALL
Java_com_aura_agent_llm_LLMService_nativeFree(JNIEnv*, jobject, jlong handle) {
    Session* session = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        auto it = g_sessions.find(handle);
        if (it == g_sessions.end()) return;
        session = it->second;
        g_sessions.erase(it);
    }
#ifdef AURA_WITH_LLAMA
    if (session->ctx != nullptr) llama_free(session->ctx);
    if (session->model != nullptr) llama_model_free(session->model);
#endif
    delete session;
}

}  // extern "C"
