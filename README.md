# Satellite Stubble Burning Detection & Analysis Platform

![GitHub repo size](https://img.shields.io/github/repo-size/your-username/stubble-burning-detection)![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)![License](https://img.shields.io/badge/License-MIT-yellow.svg)

A comprehensive geospatial intelligence platform for monitoring and analyzing agricultural fires using satellite data. This project features real-time web applications that progress from single-source detection to a high-accuracy data fusion model, and culminates in advanced analytical scripts that use satellite embeddings and atmospheric data to assess the full environmental context of burning events.

## 🌟 Project Summary

This project is a multi-stage solution for detecting and analyzing agricultural stubble burning. At its core, it harnesses the power of the VIIRS satellite sensor by utilizing two distinct data streams: the **near real-time (LANCE C2)** product for immediate hotspot detection and the **standard daily (VNP14A1)** fire product for validated analysis.

The platform evolves from simple, high-sensitivity web apps to a sophisticated **data fusion model** that combines both VIIRS streams for superior accuracy. It is complemented by powerful backend scripts that perform deep analysis, correlating fire events with burn scars, air pollution, and land-use changes identified using Google's Satellite Embeddings.

## ✨ Key Features

-   **Real-Time Fire Detection:** Interactive web dashboard for visualizing active fires using near real-time VIIRS LANCE C2 data.
-   **Validated Daily Analysis:** A separate interface optimized for analyzing daily fire patterns using the processed VIIRS VNP14A1 product.
-   **Advanced Data Fusion:** A high-accuracy model that combines both VIIRS datasets to cross-validate hotspots, reduce false positives, and improve detection confidence.
-   **Environmental Impact Analysis:** Backend scripts that integrate multiple satellite sources (VIIRS, Sentinel-2, TROPOMI) and satellite embeddings to create a holistic view of stubble burning and its atmospheric impact.
-   **Crop Classification:** An analytical script to classify crop types, providing essential context to differentiate between burning seasons (e.g., rice vs. wheat).
-   **Interactive Map Interface:** A user-friendly frontend built with Leaflet.js to display geospatial data, district boundaries, and fire intensity.
-   **Data Export:** Functionality to export detected fire hotspot data to CSV for further analysis.

## 🛠️ Tech Stack

-   **Backend:** Python, FastAPI, Google Earth Engine API
-   **Frontend:** HTML, CSS, JavaScript, Leaflet.js
-   **Core Libraries:** `uvicorn`, `pandas`, `earthengine-api`
-   **Deployment:** The application is built with FastAPI and can be served using Uvicorn.

## 📂 Project Components

The repository is structured into two main parts: the interactive web application and the advanced analytical scripts.

### 1. Web Applications (Real-Time Monitoring)

The project includes three standalone FastAPI applications that demonstrate an evolution in detection logic. The final, integrated application is run from `main.py`.

-   **`app_VIIRS_375m.py`:** A real-time fire detection app using NASA's LANCE VIIRS C2 (375m) dataset. Optimized for maximum sensitivity to detect active fire hotspots as they occur.
-   **`app_VIIRS_collection_02_1km.py`:** A specialized app for detecting agricultural stubble burning events using the NASA VIIRS VNP14A1 daily fire product. The algorithm is tuned for extreme sensitivity within agricultural areas.
-   **`app_VIIRS_combination.py`:** An advanced system that **fuses data** from both LANCE C2 and VNP14A1. This data fusion approach improves detection accuracy by cross-validating fire events.
-   **`main.py`:** The main entry point for the primary web application, which serves the frontend (`static/`) and exposes the core API endpoints.

### 2. Analytical Scripts (In-Depth Analysis)

These scripts are designed for offline analysis to extract deeper insights from the satellite data.

-   **`stubble_burning_data_embedding.py`:** A powerful script for post-event analysis. It integrates satellite embeddings (for land-use change detection), VIIRS/MODIS (for active fires), Sentinel-2 (for high-resolution burn scars), and TROPOMI (for CO & NO₂ pollution) to create a comprehensive "burning index".
-   **`crop_classification_data_embedding.py`:** A script that uses Google's Satellite Embeddings to perform crop classification over the region of interest. This helps provide context to the fire data, identifying which crop types are most associated with burning events.

## 🚀 Setup and Installation

Follow these steps to set up and run the project locally.

### Prerequisites

-   Python 3.9+
-   A Google Earth Engine account and an associated Google Cloud Project.

### 1. Clone the Repository

```bash
git clone https://github.com/deepeshyadav760/stubble-burning-detection.git
cd stubble-burning-detection
