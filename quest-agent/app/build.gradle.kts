import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing lives outside the repo. Without it, `assembleRelease` still
// builds — it just produces an unsigned artifact, which is the right failure
// for a machine that has no business holding the upload key.
val keystoreProperties = Properties().apply {
    val file = rootProject.file("keystore.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}

android {
    namespace = "com.vrcmodsuite.rosteragent"
    // 2D apps on Horizon OS may target 32-36; 29 is the oldest SDK any
    // current Quest runs, so the floor costs nothing and covers Quest 2.
    compileSdk = 34

    defaultConfig {
        applicationId = "com.vrcmodsuite.rosteragent"
        minSdk = 29
        targetSdk = 34
        versionCode = 2
        versionName = "1.0.0"
    }

    signingConfigs {
        if (keystoreProperties.getProperty("storeFile") != null) {
            create("release") {
                storeFile = file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            if (keystoreProperties.getProperty("storeFile") != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    // Reading the log folder through the Storage Access Framework, so the app
    // never asks for All-files access. See the README for why that matters
    // for store review.
    implementation("androidx.documentfile:documentfile:1.0.1")

    // The log parsing has no Android in it precisely so it can be tested on a
    // desktop — it is the only part of this that can be checked without a
    // headset, and it decides who gets screened.
    testImplementation("junit:junit:4.13.2")
}
