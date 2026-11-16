import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

// 联调地址与令牌从 local.properties 读，不进仓库。
// 缺省指向 adb reverse 的本机端口，令牌留空（服务端未配置令牌时可用）。
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.isFile) f.inputStream().use { load(it) }
}
val ootdApiBaseUrl: String = localProps.getProperty("ootd.apiBaseUrl") ?: "http://127.0.0.1:8000"
val ootdApiToken: String = localProps.getProperty("ootd.apiToken") ?: ""

android {
    namespace = "com.ootd.ai"
    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "com.ootd.ai"
        minSdk = 24
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField("String", "OOTD_API_BASE_URL", "\"$ootdApiBaseUrl\"")
        buildConfigField("String", "OOTD_API_TOKEN", "\"$ootdApiToken\"")
    }

    buildTypes {
        release {
            optimization {
                enable = true
                packageScope = setOf("androidx.**", "kotlin.**", "kotlinx.**")
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
    kotlin {
        compilerOptions {
            jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_21
        }
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.sceneview)
    testImplementation(libs.junit)
    // GlbProbe 在单元测试里要真解析 JSON；Android 的 org.json 只是抛 not-mocked 的桩，
    // 跑 JVM 测试必须换成真实实现。只在测试源集里加，不进 APK。
    testImplementation("org.json:json:20240303")
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.junit)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
    debugImplementation(libs.androidx.compose.ui.tooling)
}